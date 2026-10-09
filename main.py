#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pepper_bot — сбор сделок/промокодов pepper.ru.

Режимы:
    python main.py                  # вечный цикл: парсинг -> канал + бот с поиском
    python main.py --once           # один проход парсинга и выход
    python main.py --once --dry     # один проход, посты в stdout, ничего не слать
    python main.py --seed           # пометить текущие сделки прочитанными и выйти
    python main.py --bot            # только бот с поиском промокодов (без парсинга)
    python main.py --demo           # отправить тестовую карточку-подсказку в TG_ADMIN

Цикл парсинга: опрос лент -> новые ID -> полная карточка (фото, название,
цена было-стало, промокод из виджета, «Подробнее о скидке», комментарии,
температура) -> рендер -> sendPhoto/sendMessage в канал -> SQLite
(промокоды складываются в каталог для поиска в боте за 3/12/24 ч).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import random
import signal
import sys
from pathlib import Path

from config import CONFIG, Config
from pepper_client import PepperClient
from render import fit, render, PHOTO_CAPTION_LIMIT, TEXT_LIMIT
from storage import Store
from telegram import TelegramPoster

log = logging.getLogger("pepper")


# ---------------------------------------------------------------------- #
# Цикл парсинга
# ---------------------------------------------------------------------- #

async def run_cycle(client: PepperClient, store: Store, tg: TelegramPoster | None,
                    cfg: Config, dry: bool) -> int:
    """Один проход. Возвращает число опубликованных постов."""
    cards = await asyncio.to_thread(client.feed_cards, cfg.feeds)
    if not cards:
        log.warning("feed returned no cards — сайт мог поменять вёрстку")
        return 0

    # Первый запуск: помечаем всё существующее прочитанным, канал не спамим
    if store.is_empty() and cfg.seed_first and not dry:
        store.seed([c.deal_id for c in cards], {c.deal_id: c.title for c in cards})
        log.info("first run: %d existing deals marked as seen", len(cards))
        return 0

    fresh = [c for c in cards if store.should_process(c.deal_id)]
    fresh.sort(key=lambda c: c.deal_id, reverse=True)
    fresh = fresh[: cfg.max_per_cycle]
    if not fresh:
        return 0
    log.info("new deals: %s", [c.deal_id for c in fresh])

    posted = 0
    for card in fresh:
        try:
            deal = await asyncio.to_thread(client.get_deal, card.url, card)
        except Exception as exc:
            log.error("deal %s fetch failed: %s", card.deal_id, exc)
            store.mark_failed(card.deal_id, card.title, str(exc))
            continue

        message = fit(render(deal), PHOTO_CAPTION_LIMIT if deal.image else TEXT_LIMIT)

        if dry:
            print("=" * 70)
            print(message)
            continue

        image = await asyncio.to_thread(client.download_image, deal.image)
        if image:
            message = fit(render(deal), PHOTO_CAPTION_LIMIT)
        try:
            mode = store.get_setting("publish_mode", cfg.publish_mode)
            if mode == "review":
                if not cfg.tg_admin:
                    raise RuntimeError("review mode requires PEPPER_TG_ADMIN chat_id")
                payload = {
                    "deal_id": deal.deal_id,
                    "title": deal.title,
                    "price": str(deal.price) if deal.price is not None else "",
                    "old_price": str(deal.old_price) if deal.old_price is not None else "",
                    "promo_code": deal.promo_code,
                    "product_url": deal.product_url,
                    "body_text": deal.body_text,
                    "url": deal.url,
                    "rendered": message,
                    "image": deal.image,
                    "merchant": deal.merchant,
                }
                if image:
                    payload["image_b64"] = base64.b64encode(image).decode("ascii")
                file_id = await asyncio.to_thread(
                    tg.send_review, cfg.tg_admin, message, image, deal.deal_id, deal.product_url
                )
                if file_id:
                    payload["photo_file_id"] = file_id
                store.save_deal(payload, status="pending")
                log.info("deal %d queued for admin review", deal.deal_id)
            else:
                success, file_id = tg.post(message, image, product_url=deal.product_url,
                                           chat_id=store.get_setting("channel", cfg.tg_chat))
                payload_auto = {
                    "deal_id": deal.deal_id,
                    "title": deal.title,
                    "rendered": message,
                    "product_url": deal.product_url,
                }
                if file_id:
                    payload_auto["photo_file_id"] = file_id
                store.save_deal(payload_auto, status="published")
                store.mark_posted(deal.deal_id, deal.title)
                posted += 1
            if mode != "review" and deal.promo_code:
                store.save_promo(
                    deal.deal_id, deal.promo_code, deal.title,
                    price=deal.price, old_price=deal.old_price,
                    merchant=deal.merchant, url=deal.url,
                    product_url=deal.product_url,
                )
            if mode == "review":
                posted += 1  # карточка отправлена админу на проверку
            log.info("processed deal %d: %s", deal.deal_id, deal.title[:60])
        except Exception as exc:
            log.error("post deal %d failed: %s", deal.deal_id, exc)
            store.mark_failed(deal.deal_id, deal.title, str(exc))

        await asyncio.sleep(random.uniform(2.5, 4.0))  # антифлуд между постами
    return posted


# ---------------------------------------------------------------------- #
# Цикл бота (поиск промокодов)
# ---------------------------------------------------------------------- #

async def bot_loop(tg: TelegramPoster, store: Store, stop: asyncio.Event) -> None:
    offset = 0
    while not stop.is_set():
        try:
            offset = await asyncio.to_thread(tg.handle_updates, offset, store, CONFIG.tg_admin_ids)
        except Exception as exc:
            log.error("bot poll failed: %s", exc)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------- #
# Демо-карточка с инлайн-кнопками
# ---------------------------------------------------------------------- #

def send_demo(tg: TelegramPoster, cfg: Config) -> None:
    import json as _json
    from render import render_details

    if not cfg.tg_admin:
        log.error("нужен PEPPER_TG_ADMIN (chat_id для демо)")
        raise SystemExit(2)

    photos = [
        p.read_bytes()
        for p in sorted(Path(cfg.demo_photos_dir).glob("*.jpg"))[:2]
    ] or [
        p.read_bytes()
        for p in sorted(Path(cfg.demo_photos_dir).glob("*.png"))[:2]
    ]
    if not photos:
        log.error("нет фото в %s — положи туда скриншот подсказки (.jpg/.png)", cfg.demo_photos_dir)
        raise SystemExit(2)

    caption = render_details(
        title="Подписка WB Клуб бесплатно на 45 дней (для тех, у кого нет активной)",
        price_line="💰 <b>0 ₽</b>  <s>300 ₽</s> (100%)",
        promo_code="Wbclubosen",
        body="⚡️ Новый промокод на подписку. Активировать можно, если нет "
             "действующей подписки WB Club. После окончания 45 дней подписка "
             "автоматически продлевается на ежемесячный тариф.",
    )
    tg.send_demo_card(cfg.tg_admin, photos, caption)
    log.info("demo card sent to %s", cfg.tg_admin)


# ---------------------------------------------------------------------- #
# Точка входа
# ---------------------------------------------------------------------- #

async def main() -> int:
    parser = argparse.ArgumentParser(description="pepper.ru -> telegram deals bot")
    parser.add_argument("--once", action="store_true", help="один проход парсинга и выход")
    parser.add_argument("--dry", action="store_true", help="посты в stdout, ничего не слать")
    parser.add_argument("--seed", action="store_true", help="пометить текущее прочитанным и выйти")
    parser.add_argument("--bot", action="store_true", help="только бот с поиском промокодов")
    parser.add_argument("--demo", action="store_true", help="демо-карточка с кнопками в TG_ADMIN")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    cfg = CONFIG
    if not args.dry and not args.bot and not args.demo and not cfg.telegram_enabled:
        log.error("нет PEPPER_TG_TOKEN / PEPPER_TG_CHAT (или используй --dry)")
        return 2
    if not cfg.tg_admin_ids and not args.dry:
        log.error("нет PEPPER_TG_ADMIN_IDS: задай Telegram user ID администраторов")
        return 2

    tg = TelegramPoster(cfg.tg_token, cfg.tg_chat, proxy=cfg.telegram_proxy) if cfg.telegram_enabled else None
    store = Store(cfg.db_path)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # windows

    if args.demo:
        send_demo(tg, cfg)
        store.close()
        return 0

    client = PepperClient(
        request_delay=cfg.request_delay,
        timeout=cfg.timeout,
        pepper_proxy=cfg.pepper_proxy,
        captcha_api_key=cfg.captcha_api_key,
    )

    if args.seed:
        cards = client.feed_cards(cfg.feeds)
        store.seed([c.deal_id for c in cards], {c.deal_id: c.title for c in cards})
        client.close()
        store.close()
        return 0

    try:
        tasks: list[asyncio.Task] = []
        if not args.once and not args.dry:
            tasks.append(asyncio.create_task(bot_loop(tg, store, stop)))

        if args.bot:
            # режим --bot: крутить только бота до сигнала
            await stop.wait()
        else:
            while True:
                try:
                    n = await run_cycle(client, store, tg, cfg, args.dry)
                    log.info("cycle done, posted=%d", n)
                except Exception:
                    log.exception("cycle crashed, retry next interval")
                if args.once:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=cfg.interval + random.uniform(0, 15))
                    break  # stop signaled
                except asyncio.TimeoutError:
                    pass
    finally:
        for t in tasks:
            t.cancel()
        client.close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
