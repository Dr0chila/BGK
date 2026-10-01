from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import User
from app import services as s

router = Router()


class Reg(StatesGroup):
    name = State()


WELCOME = ("Привет, {name}! Здесь обучалка для официантов БГК: столы, обеды, сервис, вина и меню.\n"
           "Сначала учи темы, потом проходи тесты, в конце — экзамен (нужно 85%).")


async def greet_active(m: Message, bot: Bot, user: User) -> None:
    await s.enable_webapp(bot, m.chat.id)
    await m.answer(WELCOME.format(name=escape(user.full_name)), reply_markup=s.webapp_kb())


@router.message(CommandStart())
async def start(m: Message, state: FSMContext, bot: Bot, user: User | None) -> None:
    await state.clear()
    if user is None:
        await state.set_state(Reg.name)
        await m.answer("Привет! Как тебя зовут? Напиши имя и фамилию.")
    elif user.status == "pending":
        await m.answer("Заявка уже отправлена, ждём подтверждения управляющего.")
    else:
        await greet_active(m, bot, user)


@router.message(Reg.name, F.text)
async def got_name(m: Message, state: FSMContext, bot: Bot, session: AsyncSession, user: User | None) -> None:
    name = " ".join(m.text.split())
    if user is not None:  # уже зарегистрирован (двойной ввод)
        await state.clear()
        return
    if name.startswith("/") or not (3 <= len(name) <= 80):
        await m.answer("Напиши имя и фамилию обычным текстом, например: Анна Иванова.")
        return
    status = "active" if settings.registration_mode == "open" else "pending"
    new = User(tg_id=m.from_user.id, username=m.from_user.username, full_name=name,
               role="trainee", status=status)
    session.add(new)
    await session.commit()
    await state.clear()

    if status == "active":
        await greet_active(m, bot, new)
        await s.notify_admins(bot, session, f"🆕 Новый стажёр: {s.who(new)}")
        return
    await m.answer("Заявка отправлена, ждём подтверждения управляющего. Я напишу, когда доступ откроется.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"reg:ok:{new.tg_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reg:no:{new.tg_id}"),
    ]])
    await s.notify_admins(bot, session, f"🆕 Заявка на обучение: {s.who(new)}", kb)


@router.message(Reg.name)
async def name_not_text(m: Message) -> None:
    await m.answer("Напиши имя и фамилию текстом.")


@router.message(Command("me"))
async def me(m: Message, session: AsyncSession, user: User | None) -> None:
    if user is None or user.status != "active":
        await m.answer("Сначала зарегистрируйся: /start")
        return
    p = await s.progress(session, user.tg_id)
    await m.answer(f"Твои лучшие результаты:\n\n{s.progress_lines(p)}", reply_markup=s.webapp_kb())
