from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    webhook_url: str = ""
    webhook_secret: str = ""
    webapp_url: str
    database_url: str
    owner_id: int
    registration_mode: Literal["approval", "open"] = "approval"
    # polling — для локальной проверки бота без домена; на сервере webhook
    bot_mode: Literal["webhook", "polling"] = "webhook"

    exam_pass_pct: int = 85
    topics: tuple[str, ...] = ("Столы", "Обеды", "Сервис", "Вина", "Меню")


settings = Settings()
