import time
from typing import Literal

from aiogram import Bot
from aiogram.utils.web_app import safe_parse_webapp_init_data
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

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
