from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.db import Session, User


class DbMiddleware(BaseMiddleware):
    """Даёт хендлерам session и user (None, если не зарегистрирован); обновляет username."""

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        async with Session() as session:
            tg = data.get("event_from_user")
            user = await session.get(User, tg.id) if tg else None
            if user and user.status == "blocked":
                return None
            if user and user.username != tg.username:
                user.username = tg.username
                await session.commit()
            data["session"], data["user"] = session, user
            return await handler(event, data)
