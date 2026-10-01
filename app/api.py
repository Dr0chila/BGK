import time
from typing import Literal

from aiogram import Bot
from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from app import services as s
from app.config import settings
from app.db import Attempt, Session, User

router = APIRouter(prefix="/api")
MAX_AGE = 24 * 3600


def tg_user_id(x_init_data: str = Header(default="")) -> int:
    """user_id берём только из подписанного Telegram initData, никогда из тела запроса."""
    try:
        data = safe_parse_webapp_init_data(settings.bot_token, x_init_data)
    except ValueError:
        raise HTTPException(401, "bad init data")
    if data.user is None or time.time() - data.auth_date.timestamp() > MAX_AGE:
        raise HTTPException(401, "init data expired")
    return data.user.id


Str = Field(max_length=500)


class Mistake(BaseModel):
    cat: str = Str
    q: str = Str
    given: str = Str
    right: str = Str


class Result(BaseModel):
    mode: Literal["test", "exam"]
    topic: str | None = None
    score: int = Field(ge=0, le=100)
    total: int = Field(ge=1, le=100)
    pct: int = Field(ge=0, le=100)
    mistakes: list[Mistake] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def check(self):
        if self.score > self.total:
            raise ValueError("score > total")
        if self.mode == "exam":
            self.topic = None
        elif self.topic not in settings.topics:
            raise ValueError("unknown topic")
        # pct пересчитываем сами, фронту не верим
        self.pct = round(self.score * 100 / self.total)
        return self


@router.get("/me")
async def me(uid: int = Depends(tg_user_id)):
    async with Session() as session:
        u = await session.get(User, uid)
        if u is None:
            return {"registered": False}
        return {"registered": True, "name": u.full_name, "role": u.role, "status": u.status}


@router.get("/profile")
async def profile(uid: int = Depends(tg_user_id)):
    """Сырые попытки (последние 100) — аналитику по темам считает мини-апп,
    тем же кодом, что и для офлайн-истории из localStorage."""
    async with Session() as session:
        u = await session.get(User, uid)
        if u is None or u.status != "active":
            raise HTTPException(403, "not active")
        rows = await session.scalars(
            select(Attempt).where(Attempt.user_id == uid).order_by(Attempt.created_at.desc()).limit(100))
        attempts = [{"mode": a.mode, "topic": a.topic, "score": a.score, "total": a.total, "pct": a.pct,
                     "mistakes": a.mistakes, "at": a.created_at.isoformat()} for a in rows]
    return {"name": u.full_name, "attempts": attempts}


@router.post("/results")
async def results(body: Result, request: Request, uid: int = Depends(tg_user_id)):
    async with Session() as session:
        u = await session.get(User, uid)
        if u is None or u.status != "active":
            raise HTTPException(403, "not active")
        session.add(Attempt(user_id=uid, mode=body.mode, topic=body.topic, score=body.score,
                            total=body.total, pct=body.pct,
                            mistakes=[m.model_dump() for m in body.mistakes]))
        await session.commit()
        if body.mode == "exam" and body.pct >= settings.exam_pass_pct and u.role == "trainee":
            bot: Bot = request.app.state.bot
            await s.notify_admins(bot, session,
                                  f"🎓 {s.who(u)} сдал(а) экзамен: {body.score}/{body.total} ({body.pct}%)")
    return {"ok": True}


# ---------------- админка мини-аппа ----------------

async def admin_id(uid: int = Depends(tg_user_id)) -> int:
    async with Session() as session:
        u = await session.get(User, uid)
    if u is None or not u.is_admin:
        raise HTTPException(403, "admins only")
    return uid


def _iso(d):
    return d.isoformat() if d else None


@router.get("/admin/trainees")
async def admin_trainees(_: int = Depends(admin_id)):
    async with Session() as session:
        rows = await session.scalars(
            select(User).where(User.role == "trainee", User.status == "active").order_by(User.full_name))
        out = []
        for u in rows:
            p = await s.progress(session, u.tg_id)
            out.append({"id": u.tg_id, "name": u.full_name, "username": u.username,
                        "topics": p["topics"], "exam": p["exam"], "exams": p["exams"], "last": _iso(p["last"])})
    return {"topics": list(settings.topics), "pass": settings.exam_pass_pct, "items": out}


@router.get("/admin/trainees/{tg_id}")
async def admin_trainee(tg_id: int, _: int = Depends(admin_id)):
    async with Session() as session:
        u = await session.get(User, tg_id)
        if u is None:
            raise HTTPException(404)
        p = await s.progress(session, tg_id)
        last = await session.scalars(
            select(Attempt).where(Attempt.user_id == tg_id).order_by(Attempt.created_at.desc()).limit(10))
        attempts = [{"mode": a.mode, "topic": a.topic, "score": a.score, "total": a.total,
                     "pct": a.pct, "at": _iso(a.created_at)} for a in last]
        top = await s.top_mistakes(session, tg_id)
    return {"id": u.tg_id, "name": u.full_name, "username": u.username, "topics": p["topics"],
            "exam": p["exam"], "exams": p["exams"], "last": _iso(p["last"]), "attempts": attempts,
            "mistakes": [{"q": q, "n": n} for q, n in top]}


@router.get("/admin/pending")
async def admin_pending(_: int = Depends(admin_id)):
    async with Session() as session:
        rows = await session.scalars(select(User).where(User.status == "pending").order_by(User.created_at))
        return {"items": [{"id": u.tg_id, "name": u.full_name, "username": u.username,
                           "at": _iso(u.created_at)} for u in rows]}


class Decision(BaseModel):
    approve: bool


@router.post("/admin/pending/{tg_id}")
async def admin_decide(tg_id: int, body: Decision, request: Request, uid: int = Depends(admin_id)):
    async with Session() as session:
        by = await session.get(User, uid)
        text = await s.decide_application(request.app.state.bot, session, tg_id, body.approve, by)
    if text is None:
        raise HTTPException(409, "already decided")
    return {"ok": True}
