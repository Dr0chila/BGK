import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager, suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent, Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from app import services as s
from app.api import router as api_router
from app.bot import admin, trainee
from app.bot.middleware import DbMiddleware
from app.config import settings
from app.db import Session

ROOT = Path(__file__).resolve().parent.parent
WEBAPP = ROOT / "webapp" / "index.html"

# Консоль → journald (systemd), файл — чтобы логи пережили ротацию журнала.
# 10 МБ × 5 файлов: без ротации bot.log за пару месяцев съедает диск.
_handlers: list[logging.Handler] = [logging.StreamHandler()]
with suppress(OSError):
    _handlers.append(RotatingFileHandler(ROOT / "bot.log", maxBytes=10 * 1024 * 1024,
                                         backupCount=5, encoding="utf-8"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=_handlers)
log = logging.getLogger("bgk")

bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
dp.update.outer_middleware(DbMiddleware())
dp.include_routers(admin.router, trainee.router)

WEBHOOK_CHECK_SEC = 600
ALERT_EVERY_SEC = 300
_last_alert = 0.0


async def alert_owner(msg: str) -> None:
    """Ошибки — владельцу в личку, не чаще раза в 5 минут, чтобы не завалить при каскаде."""
    global _last_alert
    if time.monotonic() - _last_alert < ALERT_EVERY_SEC:
        return
    _last_alert = time.monotonic()
    with suppress(Exception):
        await bot.send_message(settings.owner_id, f"⚠️ БГК-бот: {msg[:3500]}", parse_mode=None)


@dp.errors()
async def on_error(event: ErrorEvent) -> bool:
    u = event.update
    who = (u.message and u.message.from_user) or (u.callback_query and u.callback_query.from_user)
    log.exception("update_id=%s user=%s", u.update_id, who and who.id, exc_info=event.exception)
    # Человек не должен остаться с «часиками» на кнопке и тишиной в ответ
    with suppress(Exception):
        if u.callback_query:
            await u.callback_query.answer("Что-то пошло не так, попробуй ещё раз.", show_alert=True)
        elif u.message:
            await u.message.answer("Что-то пошло не так, попробуй ещё раз чуть позже.")
    await alert_owner(f"{type(event.exception).__name__}: {event.exception}")
    return True


async def set_webhook() -> None:
    await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret,
                          allowed_updates=dp.resolve_used_update_types())


async def webhook_guard() -> None:
    """Webhook могут снести (кто-то запустил копию бота в polling, сменили токен) —
    тогда бот молча глохнет. Раз в 10 минут сверяем и чиним."""
    while True:
        await asyncio.sleep(WEBHOOK_CHECK_SEC)
        try:
            info = await bot.get_webhook_info()
            if info.url != settings.webhook_url:
                log.warning("webhook сбит (%r), восстанавливаю", info.url)
                await set_webhook()
                await alert_owner(f"webhook был сбит ({info.url or 'пусто'}), восстановлен")
            elif info.last_error_message and info.pending_update_count > 50:
                log.warning("webhook: %s, в очереди %s", info.last_error_message, info.pending_update_count)
        except Exception:
            log.exception("webhook_guard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bot = bot
    async with Session() as session:
        await s.ensure_owner(session)
    await bot.set_my_commands([BotCommand(command="start", description="Начать / открыть обучалку"),
                               BotCommand(command="me", description="Мои результаты")])
    # drop_pending_updates не ставим: что прислали, пока бот перезапускался, должно дойти
    if settings.bot_mode == "webhook":
        await set_webhook()
        task = asyncio.create_task(webhook_guard())
    else:
        await bot.delete_webhook()
        task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    log.info("BGK bot started, mode=%s", settings.bot_mode)
    yield
    if settings.bot_mode == "polling":
        with suppress(Exception):
            await dp.stop_polling()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await bot.session.close()
    log.info("BGK bot stopped")


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(api_router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("API %s %s", request.method, request.url.path, exc_info=exc)
    await alert_owner(f"API {request.url.path}: {type(exc).__name__}: {exc}")
    return JSONResponse({"ok": False}, status_code=500)


@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if not settings.webhook_secret or not secrets.compare_digest(x_telegram_bot_api_secret_token, settings.webhook_secret):
        raise HTTPException(403)
    # Всегда 200: на ошибку Telegram шлёт тот же апдейт снова и снова, и одно
    # битое сообщение тормозит всю очередь. Ошибки хендлеров уже залогировал on_error.
    try:
        update = Update.model_validate(await request.json(), context={"bot": bot})
    except Exception:
        log.exception("webhook: не разобрал апдейт")
        return {"ok": True}
    with suppress(Exception):
        await dp.feed_update(bot, update)
    return {"ok": True}


@app.get("/app")
async def webapp():
    return FileResponse(WEBAPP, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    """Для deploy.sh и внешнего мониторинга: живы процесс и база."""
    try:
        async with Session() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"ok": False, "db": False}, status_code=503)
    return {"ok": True}
