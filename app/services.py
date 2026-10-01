"""Запросы к БД и общие действия, которые нужны и боту, и API."""
from collections import Counter
from datetime import datetime
from html import escape

from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
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


# Подсказки после «/»: у каждой роли свой список (scope на конкретный чат)
TRAINEE_CMDS = [
    BotCommand(command="start", description="Открыть обучалку"),
    BotCommand(command="me", description="Мои результаты"),
    BotCommand(command="help", description="Что умеет бот"),
]
ADMIN_CMDS = TRAINEE_CMDS + [
    BotCommand(command="stats", description="Стажёры и прогресс"),
    BotCommand(command="trainee", description="Подробно по стажёру: /trainee @ник"),
    BotCommand(command="pending", description="Заявки на доступ"),
    BotCommand(command="delete", description="Удалить аккаунт: /delete @ник"),
]
# Должности назначают владелец и менеджеры
OWNER_CMDS = ADMIN_CMDS + [
    BotCommand(command="position", description="Назначить должность: /position @ник"),
]


# ---------- должности ----------
POSITIONS = {"manager": "Менеджер", "senior": "Старший официант", "waiter": "Официант", "trainee": "Стажёр"}
ADMIN_POSITIONS = {"manager", "senior"}          # дают доступ к админке
_ALIASES = {"manager": ("мен", "менеджер", "manager", "упр", "управляющий"),
            "senior": ("старший", "старш", "старший официант", "старш оф", "ст оф", "senior"),
            "waiter": ("официант", "оф", "waiter"),
            "trainee": ("стажёр", "стажер", "trainee")}


def parse_position(text: str) -> str | None:
    t = " ".join(text.lower().replace("ё", "е").replace(".", " ").split())
    for key, names in _ALIASES.items():
        if t in (n.replace("ё", "е") for n in names):
            return key
    return None


def title(u: User) -> str:
    if u.role == "owner":
        return "Владелец"
    if u.position in POSITIONS:
        return POSITIONS[u.position]
    return "Админ" if u.role == "admin" else "Стажёр"


def can_assign(actor: User, target: User, pos: str) -> bool:
    """Владелец — любую. Менеджер — старшего, официанта, стажёра, и не трогает других менеджеров."""
    if target.role == "owner" or actor.tg_id == target.tg_id:
        return False
    if actor.role == "owner":
        return True
    return actor.position == "manager" and pos != "manager" and target.position != "manager"


def assignable(actor: User) -> list[str]:
    if actor.role == "owner":
        return list(POSITIONS)
    return ["senior", "waiter", "trainee"] if actor.position == "manager" else []


def is_trainee(u: User) -> bool:
    """Стажёр — пока не переведён в официанты (старые записи без должности — тоже стажёры)."""
    return u.role == "trainee" and u.position in (None, "trainee")


# SQL-условие «стажёр» — для списков /stats и админки
TRAINEE_SQL = (User.role == "trainee") & ((User.position == None) | (User.position == "trainee"))  # noqa: E711


def can_delete(actor: User, target: User) -> bool:
    """Владелец — любого; менеджер — всех, кроме менеджеров; старший — только стажёров."""
    if target.role == "owner" or actor.tg_id == target.tg_id:
        return False
    if actor.role == "owner":
        return True
    if actor.position == "manager":
        return target.position != "manager"
    return actor.position == "senior" and is_trainee(target)


async def exam_best(session: AsyncSession, uid: int) -> int | None:
    return await session.scalar(select(func.max(Attempt.pct)).where(Attempt.user_id == uid, Attempt.mode == "exam"))


async def promote(bot: Bot, session: AsyncSession, actor: User, target: User) -> str | None:
    """Стажёр → официант после сданного экзамена; подтверждает любой админ.
    Возвращает текст ошибки или None при успехе."""
    if not (actor.role == "owner" or actor.is_admin):
        return "Переводить могут владелец, менеджер или старший официант."
    if not is_trainee(target):
        return f"{target.full_name} уже не стажёр ({title(target).lower()})."
    best = await exam_best(session, target.tg_id)
    if best is None or best < settings.exam_pass_pct:
        return f"Экзамен ещё не сдан (нужно {settings.exam_pass_pct}%)."
    await set_position(bot, session, target, "waiter")
    try:
        await bot.send_message(target.tg_id, "🎉 Поздравляем! Ты переведён(а) в <b>официанты</b>.")
    except Exception:
        pass
    return None


async def delete_user(session: AsyncSession, target: User) -> None:
    # попытки удалятся каскадом (FK ondelete=CASCADE)
    await session.delete(target)
    await session.commit()


def promote_kb(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Перевести в официанты", callback_data=f"promote:{tg_id}")]])


async def set_position(bot: Bot, session: AsyncSession, target: User, pos: str) -> None:
    target.position = pos
    target.role = "admin" if pos in ADMIN_POSITIONS else "trainee"
    if pos in ADMIN_POSITIONS:
        target.status = "active"
    await session.commit()
    await sync_commands(bot, target)


async def sync_commands(bot: Bot, user: User) -> None:
    """Админам и владельцу — расширенный список; стажёрам — общий (default)."""
    scope = BotCommandScopeChat(chat_id=user.tg_id)
    try:
        if user.role == "owner" or (user.role == "admin" and user.position == "manager"):
            await bot.set_my_commands(OWNER_CMDS, scope=scope)
        elif user.role == "admin":
            await bot.set_my_commands(ADMIN_CMDS, scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except Exception:
        pass  # чат ещё не начат с ботом — подсказки поставятся при следующем старте


async def setup_commands(bot: Bot, session: AsyncSession) -> None:
    await bot.set_my_commands(TRAINEE_CMDS, scope=BotCommandScopeDefault())
    for a in await admins(session):
        await sync_commands(bot, a)


async def decide_application(bot: Bot, session: AsyncSession, tg_id: int, approve: bool,
                             by: User) -> str | None:
    """Принять/отклонить заявку — общее для кнопок в боте и админки в мини-аппе.
    None — заявки уже нет (разобрал другой админ)."""
    target = await session.get(User, tg_id)
    if target is None or target.status != "pending":
        return None
    if approve:
        target.status = "active"
        await session.commit()
        try:
            await enable_webapp(bot, tg_id)
            await bot.send_message(tg_id, "Доступ открыт! Жми кнопку ниже.", reply_markup=webapp_kb())
        except Exception:
            pass
        return f"✅ Принят: {who(target)} — решил(а) {who(by)}"
    # удаляем, чтобы человек мог подать заявку заново (например, с правильным именем)
    text = f"❌ Отклонён: {who(target)} — решил(а) {who(by)}"
    await session.delete(target)
    await session.commit()
    try:
        await bot.send_message(tg_id, "Заявка отклонена. Если это ошибка — напиши управляющему.")
    except Exception:
        pass
    return text


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
