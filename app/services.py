"""Запросы к БД и общие действия, которые нужны и боту, и API."""
from collections import Counter
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    WebAppInfo,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import Attempt, User


def webapp_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📚 Открыть обучалку", web_app=WebAppInfo(url=settings.webapp_url))
    ]])


async def enable_webapp(bot: Bot, chat_id: int) -> None:
    await bot.set_chat_menu_button(
        chat_id=chat_id,
        menu_button=MenuButtonWebApp(text="Обучалка", web_app=WebAppInfo(url=settings.webapp_url)),
    )


async def ensure_owner(session: AsyncSession) -> None:
    owner = await session.get(User, settings.owner_id)
    if owner is None:
        session.add(User(tg_id=settings.owner_id, full_name="Владелец", role="owner", status="active"))
    else:
        owner.role, owner.status = "owner", "active"
    await session.commit()


async def by_username(session: AsyncSession, raw: str) -> User | None:
    name = raw.strip().lstrip("@").lower()
    if not name:
        return None
    return await session.scalar(select(User).where(func.lower(User.username) == name))


async def admins(session: AsyncSession) -> list[User]:
    rows = await session.scalars(
        select(User).where(User.role.in_(("admin", "owner")), User.status == "active")
    )
    return list(rows)


async def notify_admins(bot: Bot, session: AsyncSession, text: str,
                        kb: InlineKeyboardMarkup | None = None) -> None:
    for a in await admins(session):
        try:
            await bot.send_message(a.tg_id, text, reply_markup=kb)
        except Exception:
            pass  # админ мог не запускать бота или заблокировать его


def who(u: User) -> str:
    tail = f" (@{escape(u.username)})" if u.username else ""
    return f"<b>{escape(u.full_name)}</b>{tail}"


def fmt_date(d: datetime | None) -> str:
    return d.strftime("%d.%m %H:%M") if d else "—"


async def progress(session: AsyncSession, user_id: int) -> dict:
    """Лучший % по каждой теме, лучший экзамен, число попыток, последняя активность."""
    rows = await session.execute(
        select(Attempt.mode, Attempt.topic, func.max(Attempt.pct), func.count(), func.max(Attempt.created_at))
        .where(Attempt.user_id == user_id)
        .group_by(Attempt.mode, Attempt.topic)
    )
    topics, exam, exams, last = {}, None, 0, None
    for mode, topic, best, cnt, at in rows:
        if mode == "exam":
            exam, exams = best, cnt
        else:
            topics[topic] = best
        last = max(last, at) if last else at
    return {"topics": topics, "exam": exam, "exams": exams, "last": last}


def progress_lines(p: dict) -> str:
    def mark(v):
        return "—" if v is None else f"{v}%" + (" ✅" if v >= settings.exam_pass_pct else "")
    lines = [f"{t}: {mark(p['topics'].get(t))}" for t in settings.topics]
    lines.append(f"Экзамен: {mark(p['exam'])}" + (f" (попыток: {p['exams']})" if p["exams"] else ""))
    return "\n".join(lines)


async def top_mistakes(session: AsyncSession, user_id: int, limit: int = 10) -> list[tuple[str, int]]:
    rows = await session.scalars(select(Attempt.mistakes).where(Attempt.user_id == user_id))
    c = Counter(m.get("q", "?") for ms in rows for m in ms)
    return c.most_common(limit)
