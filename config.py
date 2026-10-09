# -*- coding: utf-8 -*-
"""Конфигурация pepper_bot.

Всё читается из переменных окружения. Файл .env рядом с проектом
подхватывается автоматически (простой парсер без внешних зависимостей).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


_load_dotenv(Path(__file__).resolve().parent / ".env")


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # Telegram
    tg_token: str = os.environ.get("PEPPER_TG_TOKEN", "")
    tg_chat: str = os.environ.get("PEPPER_TG_CHAT", "")  # @channel_name или -100...
    # Основной цикл
    interval: float = _env_float("PEPPER_INTERVAL", 180.0)   # сек между опросами
    max_per_cycle: int = _env_int("PEPPER_MAX_PER_CYCLE", 5)  # лимит постов за цикл (антифлуд TG)
    # Первая инициализация: пометить текущие сделки прочитанными, не постя в канал
    seed_first: bool = _env_bool("PEPPER_SEED_FIRST", True)
    # Хранилище
    db_path: str = os.environ.get(
        "PEPPER_DB_PATH",
        str(Path(__file__).resolve().parent / "pepper_seen.sqlite3"),
    )
    # Сеть
    request_delay: float = _env_float("PEPPER_REQUEST_DELAY", 2.0)  # пауза между запросами
    timeout: float = _env_float("PEPPER_TIMEOUT", 30.0)
    pepper_proxy: str = os.environ.get("PEPPER_PROXY", "")  # прокси для pepper.ru (пусто для РФ сервера)
    telegram_proxy: str = os.environ.get("PEPPER_TG_PROXY", "")  # прокси для Telegram API (обязателен в РФ)
    # 2captcha для обхода Yandex SmartCaptcha
    captcha_api_key: str = os.environ.get("PEPPER_2CAPTCHA_KEY", "")
    # Бот: chat_id админа для демо/диагностики
    tg_admin: str = os.environ.get("PEPPER_TG_ADMIN", "")
    tg_admin_ids: tuple[int, ...] = tuple(
        int(value.strip()) for value in os.environ.get("PEPPER_TG_ADMIN_IDS", "").split(",")
        if value.strip().lstrip("-").isdigit()
    ) or tuple(
        int(value.strip()) for value in os.environ.get("PEPPER_TG_ADMIN", "").split(",")
        if value.strip().lstrip("-").isdigit()
    )
    publish_mode: str = os.environ.get("PEPPER_PUBLISH_MODE", "auto").strip().lower()
    # Папка со скриншотами подсказки для --demo
    demo_photos_dir: str = os.environ.get(
        "PEPPER_DEMO_PHOTOS",
        str(Path(__file__).resolve().parent / "demo_photos"),
    )
    # Ленты для мониторинга
    feeds: tuple[str, ...] = (
        "https://www.pepper.ru/deals",
        "https://www.pepper.ru/new",
    )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_token and self.tg_chat)


CONFIG = Config()
