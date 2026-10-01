from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import services as s
from app.config import settings
from app.db import Attempt, User

PAGE = 8


class Role(BaseFilter):
    def __init__(self, *roles: str) -> None:
        self.roles = roles

    async def __call__(self, event: TelegramObject, user: User | None = None) -> bool:
        return bool(user and user.status == "active" and user.role in self.roles)


admin = Router()
admin.message.filter(Role("admin", "owner"))
admin.callback_query.filter(Role("admin", "owner"))

owner = Router()
owner.message.filter(Role("owner"))

router = Router()
router.include_routers(owner, admin)


# ---------- заявки ----------

@admin.message(Command("pending"))
async def pending(m: Message, session: AsyncSession) -> None:
    rows = list(await session.scalars(select(User).where(User.status == "pending").order_by(User.created_at)))
    if not rows:
        await m.answer("Заявок нет.")
        return
    for u in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Принять", callback_data=f"reg:ok:{u.tg_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reg:no:{u.tg_id}"),
        ]])
        await m.answer(f"Заявка: {s.who(u)}, {s.fmt_date(u.created_at)}", reply_markup=kb)


@admin.callback_query(F.data.startswith("reg:"))
async def decide(c: CallbackQuery, bot: Bot, session: AsyncSession, user: User) -> None:
    _, action, raw_id = c.data.split(":")
    target = await session.get(User, int(raw_id))
    if target is None or target.status != "pending":
        await c.answer("Заявку уже разобрали.", show_alert=True)
        await c.message.edit_reply_markup(reply_markup=None)
        return
    if action == "ok":
        target.status = "active"
        await session.commit()
        await c.message.edit_text(f"✅ Принят: {s.who(target)} — решил(а) {s.who(user)}")
        try:
            await s.enable_webapp(bot, target.tg_id)
            await bot.send_message(target.tg_id, "Доступ открыт! Жми кнопку ниже.", reply_markup=s.webapp_kb())
        except Exception:
            pass
    else:
        # удаляем, чтобы человек мог подать заявку заново (например, с правильным именем)
        text = f"❌ Отклонён: {s.who(target)} — решил(а) {s.who(user)}"
        await session.delete(target)
        await session.commit()
        await c.message.edit_text(text)
        try:
            await bot.send_message(int(raw_id), "Заявка отклонена. Если это ошибка — напиши управляющему.")
        except Exception:
            pass
    await c.answer()


# ---------- статистика ----------

async def stats_page(session: AsyncSession, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    total = await session.scalar(
        select(func.count()).select_from(User).where(User.role == "trainee", User.status == "active"))
    if not total:
        return "Активных стажёров пока нет.", None
    pages = (total + PAGE - 1) // PAGE
    page = max(0, min(page, pages - 1))
    rows = await session.scalars(
        select(User).where(User.role == "trainee", User.status == "active")
        .order_by(User.full_name).offset(page * PAGE).limit(PAGE))
    blocks = []
    for u in rows:
        p = await s.progress(session, u.tg_id)
        topics = " · ".join(f"{t[:4]} {p['topics'][t]}%" for t in p["topics"] if t) or "тестов нет"
        exam = "—" if p["exam"] is None else f"{p['exam']}%" + (" ✅" if p["exam"] >= settings.exam_pass_pct else "")
        blocks.append(f"{s.who(u)}\n{topics}\nЭкзамен: {exam} · был(а): {s.fmt_date(p['last'])}")
    text = f"Стажёры ({total}), стр. {page + 1}/{pages}:\n\n" + "\n\n".join(blocks) + \
           "\n\nПодробно: /trainee @username"
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"stats:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"stats:{page + 1}"))
    return text, InlineKeyboardMarkup(inline_keyboard=[nav]) if nav else None


@admin.message(Command("stats"))
async def stats(m: Message, session: AsyncSession) -> None:
    text, kb = await stats_page(session, 0)
    await m.answer(text, reply_markup=kb)


@admin.callback_query(F.data.startswith("stats:"))
async def stats_nav(c: CallbackQuery, session: AsyncSession) -> None:
    text, kb = await stats_page(session, int(c.data.split(":")[1]))
    await c.message.edit_text(text, reply_markup=kb)
    await c.answer()


@admin.message(Command("trainee"))
async def trainee(m: Message, command: CommandObject, session: AsyncSession) -> None:
    if not command.args:
        await m.answer("Формат: /trainee @username")
        return
    u = await s.by_username(session, command.args)
    if u is None:
        await m.answer("Не нашёл. Человек должен хотя бы раз нажать /start, и у него должен быть username.")
        return
    p = await s.progress(session, u.tg_id)
    last = await session.scalars(
        select(Attempt).where(Attempt.user_id == u.tg_id).order_by(Attempt.created_at.desc()).limit(10))
    att = "\n".join(
        f"{s.fmt_date(a.created_at)} — {'Экзамен' if a.mode == 'exam' else a.topic}: {a.score}/{a.total} ({a.pct}%)"
        for a in last) or "попыток нет"
    top = await s.top_mistakes(session, u.tg_id)
    mis = "\n".join(f"{n}× {escape(q)}" for q, n in top) or "ошибок нет"
    await m.answer(f"{s.who(u)} · {u.role}, {u.status}\n\n<b>Лучшие результаты</b>\n{s.progress_lines(p)}"
                   f"\n\n<b>Последние попытки</b>\n{att}\n\n<b>Частые ошибки</b>\n{mis}")


# ---------- роли (только владелец) ----------

async def set_role(m: Message, command: CommandObject, session: AsyncSession, role: str) -> None:
    if not command.args:
        await m.answer(f"Формат: /{command.command} @username")
        return
    u = await s.by_username(session, command.args)
    if u is None:
        await m.answer("Не нашёл. Человек должен сначала нажать /start в боте.")
        return
    if u.role == "owner":
        await m.answer("Роль владельца не меняется.")
        return
    u.role = role
    if role == "admin":
        u.status = "active"
    await session.commit()
    await m.answer(f"{s.who(u)} теперь {'админ' if role == 'admin' else 'стажёр'}.")


@owner.message(Command("makeadmin"))
async def makeadmin(m: Message, command: CommandObject, session: AsyncSession) -> None:
    await set_role(m, command, session, "admin")


@owner.message(Command("removeadmin"))
async def removeadmin(m: Message, command: CommandObject, session: AsyncSession) -> None:
    await set_role(m, command, session, "trainee")


@admin.message(Command("help"))
async def admin_help(m: Message, user: User) -> None:
    text = ("/stats — стажёры и прогресс\n/trainee @username — подробно по стажёру\n"
            "/pending — заявки на доступ\n/me — свои результаты")
    if user.role == "owner":
        text += "\n/makeadmin @username · /removeadmin @username"
    await m.answer(text)
