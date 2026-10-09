# -*- coding: utf-8 -*-
"""Telegram Bot API: постинг в канал + long-poll бот с поиском промокодов.

Канал: sendPhoto (готовая карточка) или sendMessage.
Бот: reply-клавиатура «🎟 Промокоды», инлайн-выбор периода 3/12/24 ч,
инлайн-кнопки под медиагруппой «Показать карточку» / «Показать данные».

Все вызовы синхронные (curl_cffi), из асинхронного кода — через
asyncio.to_thread, чтобы не блокировать цикл парсинга.
"""
from __future__ import annotations

import logging
import base64
import time

from curl_cffi import CurlMime
from curl_cffi import requests as cr

log = logging.getLogger("pepper.tg")

API = "https://api.telegram.org/bot{token}/{method}"


def _kb_promo_button() -> dict:
    return {"keyboard": [[{"text": "🎟 Промокоды"}]], "resize_keyboard": True}


def _ikb_card() -> dict:
    return {
        "inline_keyboard": [[
            {"text": "🃏 Показать карточку", "callback_data": "show_card"},
            {"text": "📄 Показать данные", "callback_data": "show_data"},
        ]]
    }


def _ikb_product(url: str | None) -> dict | None:
    if not url:
        return None
    return {"inline_keyboard": [[{"text": "🛒 Перейти к товару", "url": url}]]}


def _ikb_main(mode: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "📊 Статистика", "callback_data": "menu:stats"},
         {"text": "⚠️ Ошибки", "callback_data": "menu:errors"}],
        [{"text": "📝 Очередь модерации", "callback_data": "menu:pending"}],
        [{"text": f"⚙️ Режим: {mode}", "callback_data": "menu:mode"},
         {"text": "📣 Канал", "callback_data": "menu:channel"}],
    ]}


def _ikb_back(target: str = "main") -> dict:
    return {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": f"menu:{target}"}]]}


def _ikb_mode(mode: str) -> dict:
    return {"inline_keyboard": [
        [{"text": ("✅ " if mode == "auto" else "") + "Автоматически", "callback_data": "mode:auto"}],
        [{"text": ("✅ " if mode == "review" else "") + "После подтверждения", "callback_data": "mode:review"}],
        [{"text": "⬅️ Назад", "callback_data": "menu:main"}],
    ]}


def _ikb_review(deal_id: int, product_url: str | None) -> dict:
    rows = [[
        {"text": "✅ Принять и опубликовать", "callback_data": f"approve:{deal_id}"},
        {"text": "⏭ Пропустить", "callback_data": f"skip:{deal_id}"},
    ], [
        {"text": "✏️ Редактировать", "callback_data": f"edit:{deal_id}"},
    ]]
    if product_url:
        rows.append([{"text": "🛒 Проверить товар", "url": product_url}])
    return {"inline_keyboard": rows}


class TelegramPoster:
    def __init__(self, token: str, chat_id: str, timeout: float = 30.0, proxy: str | None = None) -> None:
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.proxy = proxy

    def _call(self, method: str, retries: int = 2, **kwargs) -> dict:
        url = API.format(token=self.token, method=method)
        request_timeout = kwargs.pop("request_timeout", self.timeout)

        # Добавить прокси если настроен (для обхода блокировки Telegram в РФ)
        if self.proxy:
            # curl_cffi accepts a single HTTP CONNECT proxy via ``proxy``.
            # Passing a requests-style ``proxies`` mapping can silently route
            # HTTPS requests incorrectly in some curl_cffi versions.
            kwargs.setdefault("proxy", self.proxy)

        multipart_parts = kwargs.pop("multipart", None)

        for attempt in range(retries + 1):
            multipart = None
            try:
                if multipart_parts is not None:
                    # curl_cffi intentionally does not implement requests' ``files``
                    # argument.  Multipart forms must be sent as CurlMime.
                    multipart = self._multipart(multipart_parts)
                    r = cr.post(url, multipart=multipart, timeout=request_timeout, **kwargs)
                else:
                    r = cr.post(url, timeout=request_timeout, **kwargs)
            except Exception:
                if multipart is not None:
                    multipart.close()
                if attempt == retries:
                    raise
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                data = r.json()
            finally:
                if multipart is not None:
                    multipart.close()
            if data.get("ok"):
                return data
            # Telegram считает повторное редактирование тем же сообщением
            # ошибкой 400. Для навигации это нормальный идемпотентный исход.
            if (
                method in ("editMessageText", "editMessageCaption", "editMessageReplyMarkup")
                and data.get("error_code") == 400
                and "message is not modified" in str(data.get("description", "")).lower()
            ):
                return data
            if data.get("error_code") == 429 and attempt < retries:
                time.sleep(float(data.get("parameters", {}).get("retry_after", 2)) + 0.5)
                continue
            raise RuntimeError(f"TG {method}: {data.get('error_code')} {data.get('description')}")
        raise RuntimeError(f"TG {method}: retries exhausted")

    @staticmethod
    def _multipart(parts: dict) -> CurlMime:
        """Build a curl_cffi multipart form.

        Values are either plain form values or ``(filename, data, mime_type)``
        for uploaded files.  Keeping construction here avoids accidentally
        passing the unsupported ``files=`` argument to curl_cffi.
        """
        form = CurlMime()
        for name, value in parts.items():
            if isinstance(value, tuple) and len(value) == 3:
                filename, data, content_type = value
                form.addpart(
                    name=name,
                    filename=filename,
                    content_type=content_type,
                    data=data,
                )
            else:
                if isinstance(value, tuple):
                    value = value[-1]
                if not isinstance(value, bytes):
                    value = str(value).encode("utf-8")
                form.addpart(name=name, content_type="text/plain", data=value)
        return form

    # ------------------------------------------------------------------ #
    # Постинг в канал
    # ------------------------------------------------------------------ #

    def post(self, text: str, image: bytes | None = None,
             product_url: str | None = None, chat_id: str | int | None = None,
             photo_file_id: str | None = None) -> tuple[bool, str | None]:
        """Готовая карточка в канал. Возвращает (опубликовано, file_id фото или None)."""
        keyboard = _ikb_product(product_url)
        import json as _json
        destination = chat_id if chat_id is not None else self.chat_id
        if photo_file_id:
            try:
                self._call("sendPhoto", data={
                    "chat_id": destination, "photo": photo_file_id, "caption": text,
                    "parse_mode": "HTML",
                    **({"reply_markup": _json.dumps(keyboard)} if keyboard else {}),
                })
                return True, photo_file_id
            except Exception as exc:
                log.warning("sendPhoto by file_id failed, trying upload: %s", exc)
        if image:
            try:
                multipart_data = {
                    "chat_id": destination,
                    "caption": text,
                    "parse_mode": "HTML",
                    "photo": ("deal.jpg", image, "image/jpeg"),
                }
                if keyboard:
                    multipart_data["reply_markup"] = _json.dumps(keyboard)
                result = self._call("sendPhoto", multipart=multipart_data)
                photos = result.get("result", {}).get("photo", [])
                file_id = photos[-1].get("file_id") if photos else None
                return True, file_id
            except Exception as exc:
                log.warning("sendPhoto failed (%s), fallback to sendMessage", exc)
        self._call(
            "sendMessage",
            data={
                "chat_id": destination,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                **({"reply_markup": _json.dumps(keyboard)} if keyboard else {}),
            },
        )
        return True, None

    def send_review(self, chat_id: str | int, text: str, image: bytes | None,
                    deal_id: int, product_url: str | None = None,
                    photo_file_id: str | None = None) -> str | None:
        import json as _json
        markup = _json.dumps(_ikb_review(deal_id, product_url))
        if photo_file_id:
            try:
                self._call("sendPhoto", data={
                    "chat_id": chat_id, "photo": photo_file_id, "caption": text,
                    "parse_mode": "HTML", "reply_markup": markup,
                })
                return photo_file_id
            except Exception as exc:
                log.warning("review photo file_id failed, sending text: %s", exc)
        if image:
            multipart_data = {
                "chat_id": chat_id,
                "caption": text,
                "parse_mode": "HTML",
                "reply_markup": markup,
                "photo": ("deal.jpg", image, "image/jpeg"),
            }
            data = self._call("sendPhoto", multipart=multipart_data)
            photos = data.get("result", {}).get("photo", [])
            return photos[-1].get("file_id") if photos else None
        else:
            self._call("sendMessage", data={"chat_id": chat_id, "text": text,
                                             "parse_mode": "HTML", "reply_markup": markup,
                                             "disable_web_page_preview": True})
            return None

    # ------------------------------------------------------------------ #
    # Бот: long-poll
    # ------------------------------------------------------------------ #

    def get_updates(self, offset: int, timeout_s: int = 25) -> list[dict]:
        data = self._call("getUpdates", request_timeout=timeout_s + 10,
                          data={"offset": offset, "timeout": timeout_s})
        return data.get("result", [])

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", data={"callback_query_id": callback_id, "text": text})
        except Exception as exc:
            log.warning("answerCallbackQuery failed: %s", exc)

    def send_bot_message(self, chat_id, text: str, reply_markup: dict | None = None) -> dict:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            import json as _json
            payload["reply_markup"] = _json.dumps(reply_markup)
        return self._call("sendMessage", data=payload)

    def set_my_commands(self) -> None:
        """Убирает старый командный интерфейс из меню Telegram."""
        self._call("setMyCommands", data={"commands": "[]"})

    def edit_bot_message(self, chat_id, message_id: int, text: str,
                         reply_markup: dict | None = None) -> None:
        import json as _json
        data = {"chat_id": chat_id, "message_id": message_id,
                "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if reply_markup is not None:
            data["reply_markup"] = _json.dumps(reply_markup)
        self._call("editMessageText", data=data)

    def send_photo_by_url(self, chat_id, url: str, caption: str,
                          reply_markup: dict | None = None) -> dict:
        import json as _json
        data = {"chat_id": chat_id, "photo": url, "caption": caption, "parse_mode": "HTML"}
        if reply_markup is not None:
            data["reply_markup"] = _json.dumps(reply_markup)
        return self._call("sendPhoto", data=data)

    def send_photo_file_id(self, chat_id, file_id: str, caption: str,
                           reply_markup: dict | None = None) -> dict:
        import json as _json
        data = {"chat_id": chat_id, "photo": file_id, "caption": caption, "parse_mode": "HTML"}
        if reply_markup is not None:
            data["reply_markup"] = _json.dumps(reply_markup)
        return self._call("sendPhoto", data=data)

    def send_demo_card(self, chat_id, photos: list[bytes], caption: str) -> None:
        """Медиагруппа (фото подсказки) + подпись + инлайн-кнопки."""
        import json as _json
        media = [
            {"type": "photo", "media": f"attach://photo{i}",
             **({"caption": caption, "parse_mode": "HTML"} if i == 0 else {})}
            for i in range(len(photos))
        ]
        multipart_data = {f"photo{i}": (f"p{i}.jpg", blob, "image/jpeg") for i, blob in enumerate(photos)}
        multipart_data["media"] = _json.dumps(media)
        multipart_data["chat_id"] = str(chat_id)
        self._call("sendMediaGroup", multipart=multipart_data)
        self.send_bot_message(chat_id, "Что показать по этой подсказке? 👇", reply_markup=_ikb_card())

    # ------------------------------------------------------------------ #
    # Обработка апдейтов
    # ------------------------------------------------------------------ #

    def handle_updates(self, offset: int, store, admin_ids: tuple[int, ...]) -> int:
        """Обрабатывать команды только от Telegram user_id из allowlist."""
        updates = self.get_updates(offset)
        new_offset = offset
        for upd in updates:
            new_offset = max(new_offset, upd["update_id"] + 1)

            if "message" in upd:
                msg = upd["message"]
                text = (msg.get("text") or "").strip()
                chat_id = msg["chat"]["id"]
                user_id = int(msg.get("from", {}).get("id", 0))
                if user_id not in admin_ids:
                    log.warning("ignored bot message from non-admin user_id=%s", user_id)
                    continue
                if text.startswith("/start"):
                    self.send_bot_message(
                        chat_id,
                        "🐾 <b>Панель управления скидками</b>\nВыбери действие:",
                        reply_markup=_ikb_main(store.get_setting("publish_mode", "auto")),
                    )
                    try:
                        self.set_my_commands()
                    except Exception as exc:
                        log.debug("setMyCommands unsupported/failed: %s", exc)
                elif text.startswith("/whoami"):
                    self.send_bot_message(chat_id, f"Ваш Telegram user ID: <code>{user_id}</code>")
                elif text.startswith("/cancel"):
                    store.set_setting(f"edit_deal_id:{user_id}", "")
                    store.set_setting("await_channel_user", "")
                    self.send_bot_message(chat_id, "Действие отменено.", _ikb_back())
                elif text.startswith("/whoami"):
                    self.send_bot_message(chat_id, f"Ваш Telegram user ID: <code>{user_id}</code>")
                elif store.get_setting("await_channel_user") == str(user_id):
                    channel = text.strip()
                    if not (channel.startswith("@") or channel.startswith("-100")):
                        self.send_bot_message(chat_id, "Нужен @username публичного канала или числовой -100… ID.",
                                              _ikb_back("channel"))
                    else:
                        try:
                            self._call("getChat", data={"chat_id": channel})
                            store.set_setting("channel", channel)
                            store.set_setting("await_channel_user", "")
                            self.edit_bot_message(chat_id, int(store.get_setting("channel_prompt_message", "0")),
                                                  f"📣 Канал изменён: <code>{channel}</code>", _ikb_back())
                        except Exception as exc:
                            store.set_setting("await_channel_user", "")
                            self.send_bot_message(chat_id, f"Не удалось проверить канал: {exc}", _ikb_back())
                elif store.get_setting(f"edit_deal_id:{user_id}") and "|" in text:
                    deal_id = int(store.get_setting(f"edit_deal_id:{user_id}"))
                    item = store.get_record(deal_id)
                    parts = [part.strip() for part in text.split("|", 5)]
                    if not item or item["status"] != "pending" or len(parts) != 6:
                        store.set_setting(f"edit_deal_id:{user_id}", "")
                        self.send_bot_message(chat_id, "Карточка не найдена или формат неполный; используй /pending.")
                    else:
                        payload = item.get("payload") or {}
                        payload.update({"title": parts[0], "price": parts[1], "old_price": parts[2],
                                        "promo_code": parts[3], "product_url": parts[4], "body_text": parts[5]})
                        # Редактирование соответствует явно присланным полям; сохранить рендер как HTML.
                        import html as _html
                        lines = ["🐾 <b>Кот нашёл предложение</b>", f"🎁 <b>{_html.escape(parts[0])}</b>"]
                        if parts[1]:
                            try:
                                current = f"{int(float(parts[1])):,}".replace(",", " ")
                            except ValueError:
                                current = parts[1]
                            try:
                                old = f"{int(float(parts[2])):,}".replace(",", " ") if parts[2] else ""
                            except ValueError:
                                old = parts[2]
                            lines.append(f"🔥 {_html.escape(current)} ₽" + (f" вместо {_html.escape(old)} ₽" if old else ""))
                        if parts[3]:
                            lines.append(f"🎟 Промокод — <code>{_html.escape(parts[3])}</code>")
                        if parts[5]:
                            lines.extend(["", "Условия:", _html.escape(parts[5])])
                        if parts[4]:
                            lines.append(f"👉 <a href=\"{_html.escape(parts[4])}\">Ссылка на товар</a>")
                        payload["rendered"] = "\n".join(lines)
                        store.update_pending(payload)
                        store.set_setting(f"edit_deal_id:{user_id}", "")
                        self.send_review(chat_id, payload["rendered"], None, deal_id,
                                         payload.get("product_url"), payload.get("photo_file_id"))
                        self.send_bot_message(chat_id, "Карточка обновлена. Принимай или пропускай новую версию.")
                elif text.startswith("/stats"):
                    lines = ["📊 <b>Статистика</b>"]
                    for hours in (3, 12, 24, 168):
                        s = store.stats(hours)
                        label = "7 дней" if hours == 168 else f"{hours} ч"
                        lines.append(f"<b>{label}:</b> найдено {s['collected']}, опубликовано {s['published']}, "
                                     f"пропущено {s['skipped']}, ожидают {s['pending']}, ошибок {s['errors']}.")
                    self.send_bot_message(chat_id, "\n".join(lines), _ikb_back())
                elif text.startswith("/channel"):
                    channel = store.get_setting("channel", self.chat_id)
                    self.send_bot_message(chat_id,
                        f"📣 <b>Канал публикации</b>\nТекущий: <code>{channel}</code>",
                        {"inline_keyboard": [[{"text": "✏️ Сменить канал", "callback_data": "channel:set"}],
                                               [{"text": "⬅️ Назад", "callback_data": "menu:main"}]]})
                elif text.startswith("/menu"):
                    self.send_bot_message(chat_id, "🐾 <b>Панель управления скидками</b>\nВыбери действие:",
                                          _ikb_main(store.get_setting("publish_mode", "auto")))
                elif text.startswith("/errors"):
                    errors = store.recent_errors(10)
                    if not errors:
                        response = "✅ Ошибок публикации нет."
                    else:
                        response = "⚠️ <b>Последние ошибки</b>\n" + "\n".join(
                            f"• {row['deal_id']} — {row['title'][:70]}: {row['last_error'][:180]}"
                            for row in errors
                        )
                    self.send_bot_message(chat_id, response, _ikb_back())
                elif text.startswith("/mode"):
                    parts = text.split()
                    if len(parts) == 1:
                        mode = store.get_setting("publish_mode", "auto")
                        self.send_bot_message(chat_id, f"Режим публикации: <b>{mode}</b>.", _ikb_mode(mode))
                    elif parts[1] in ("auto", "review"):
                        store.set_setting("publish_mode", parts[1])
                        self.send_bot_message(chat_id, f"Режим публикации переключён: <b>{parts[1]}</b>.")
                    else:
                        self.send_bot_message(chat_id, "Используй /mode auto или /mode review")
                elif text.startswith("/pending"):
                    items = store.pending()
                    self.send_bot_message(chat_id, f"📝 <b>Очередь модерации</b>\nОжидают решения: {len(items)}", _ikb_back())
                    for item in items[:10]:
                        payload = item.get("payload") or {}
                        image = base64.b64decode(payload["image_b64"]) if payload.get("image_b64") else None
                        self.send_review(chat_id, payload.get("rendered", item.get("title", "")),
                                         image, item["deal_id"], payload.get("product_url"),
                                         payload.get("photo_file_id"))

            elif "callback_query" in upd:
                cq = upd["callback_query"]
                data = cq.get("data") or ""
                chat_id = cq["message"]["chat"]["id"]
                user_id = int(cq.get("from", {}).get("id", 0))
                if user_id not in admin_ids:
                    self.answer_callback(cq["id"], "Нет доступа")
                    log.warning("ignored callback from non-admin user_id=%s", user_id)
                    continue

                if data in ("show_card", "show_data"):
                    self.answer_callback(cq["id"])
                    self.send_bot_message(chat_id,
                        "Карточка строится из данных Pepper: фото, заголовок, цены, код/описание и ссылка на магазин.",
                        _ikb_back())
                    continue

                if data.startswith("menu:"):
                    section = data.split(":", 1)[1]
                    message_id = cq["message"]["message_id"]
                    if section == "main":
                        self.edit_bot_message(chat_id, message_id,
                                              "🐾 <b>Панель управления скидками</b>\nВыбери действие:",
                                              _ikb_main(store.get_setting("publish_mode", "auto")))
                    elif section == "stats":
                        lines = ["📊 <b>Статистика</b>"]
                        for hours in (3, 12, 24, 168):
                            stats = store.stats(hours)
                            label = "7 дней" if hours == 168 else f"{hours} ч"
                            lines.append(f"<b>{label}:</b> собрано {stats['collected']}, опубликовано {stats['published']}, "
                                         f"пропущено {stats['skipped']}, ожидают {stats['pending']}, ошибок {stats['errors']}.")
                        self.edit_bot_message(chat_id, message_id, "\n".join(lines), _ikb_back())
                    elif section == "errors":
                        errors = store.recent_errors(10)
                        text_out = "✅ Ошибок нет." if not errors else "⚠️ <b>Последние ошибки</b>\n" + "\n".join(
                            f"• {row['deal_id']} — {row['title'][:60]}: {row['last_error'][:150]}" for row in errors)
                        self.edit_bot_message(chat_id, message_id, text_out, _ikb_back())
                    elif section == "pending":
                        items = store.pending()
                        self.edit_bot_message(chat_id, message_id,
                                              f"📝 <b>Очередь модерации</b>\nКарточек ожидают: {len(items)}\n\nОтправляю карточки ниже…",
                                              _ikb_back())
                        for item in items[:10]:
                            payload = item.get("payload") or {}
                            self.send_review(chat_id, payload.get("rendered", item.get("title", "")),
                                             base64.b64decode(payload["image_b64"]) if payload.get("image_b64") else None,
                                             item["deal_id"],
                                             payload.get("product_url"), payload.get("photo_file_id"))
                    elif section == "mode":
                        mode = store.get_setting("publish_mode", "auto")
                        self.edit_bot_message(chat_id, message_id,
                                              f"⚙️ <b>Режим публикации</b>\nСейчас: {mode}", _ikb_mode(mode))
                    elif section == "channel":
                        channel = store.get_setting("channel", self.chat_id)
                        self.edit_bot_message(chat_id, message_id,
                                              f"📣 <b>Канал публикации</b>\nТекущий канал: <code>{channel}</code>\n\nНажми кнопку и отправь @username или числовой chat_id.",
                                              {"inline_keyboard": [[{"text": "✏️ Сменить канал", "callback_data": "channel:set"}],
                                                                     [{"text": "⬅️ Назад", "callback_data": "menu:main"}]]})
                    self.answer_callback(cq["id"])

                elif data.startswith("mode:"):
                    mode = data.split(":", 1)[1]
                    if mode in ("auto", "review"):
                        store.set_setting("publish_mode", mode)
                    self.answer_callback(cq["id"], "Режим сохранён")
                    self.edit_bot_message(chat_id, cq["message"]["message_id"],
                                          f"⚙️ <b>Режим публикации</b>\nСейчас: {mode}", _ikb_mode(mode))

                elif data == "channel:set":
                    store.set_setting("await_channel_user", str(user_id))
                    store.set_setting("channel_prompt_message", str(cq["message"]["message_id"]))
                    self.answer_callback(cq["id"])
                    self.edit_bot_message(chat_id, cq["message"]["message_id"],
                                          "Отправь @username публичного канала или числовой -100… ID.", _ikb_back("channel"))

                elif data.startswith("approve:"):
                    deal_id = int(data.split(":", 1)[1])
                    item = store.get_record(deal_id)
                    if not item or item["status"] != "pending":
                        self.answer_callback(cq["id"], "Карточка уже обработана")
                        continue
                    try:
                        self._call("getChat", data={"chat_id": store.get_setting("channel", self.chat_id)})
                    except Exception as exc:
                        self.answer_callback(cq["id"], "Канал недоступен")
                        self.send_bot_message(chat_id, f"Проверь канал и права бота: {exc}", _ikb_back())
                        continue
                    payload = item.get("payload") or {}
                    self.answer_callback(cq["id"], "Публикую…")
                    try:
                        success, file_id = self.post(
                            payload.get("rendered", item.get("title", "")),
                            base64.b64decode(payload["image_b64"]) if payload.get("image_b64") else None,
                            payload.get("product_url"),
                            chat_id=store.get_setting("channel", self.chat_id),
                            photo_file_id=payload.get("photo_file_id")
                        )
                        # Сохраняем file_id если получили новый (был загружен впервые)
                        if file_id and not payload.get("photo_file_id"):
                            payload["photo_file_id"] = file_id
                            store.update_pending(payload)
                        store.mark_posted(deal_id, payload.get("title", item.get("title", "")))
                        if payload.get("promo_code"):
                            def _price(value):
                                try:
                                    return float(value) if value not in (None, "") else None
                                except (TypeError, ValueError):
                                    return None
                            store.save_promo(
                                deal_id, payload["promo_code"],
                                payload.get("title", item.get("title", "")),
                                price=_price(payload.get("price")),
                                old_price=_price(payload.get("old_price")),
                                url=payload.get("url", ""),
                                product_url=payload.get("product_url"),
                            )
                        self.send_bot_message(chat_id, f"✅ Сделка {deal_id} опубликована")
                    except Exception as exc:
                        store.mark_failed(deal_id, item.get("title", ""), str(exc))
                        self.send_bot_message(chat_id, f"⚠️ Не удалось опубликовать сделку {deal_id}: {exc}")

                elif data.startswith("skip:"):
                    deal_id = int(data.split(":", 1)[1])
                    store.mark_skipped(deal_id)
                    self.answer_callback(cq["id"], "Пропущено")
                    self.send_bot_message(chat_id, f"⏭ Сделка {deal_id} пропущена")

                elif data.startswith("edit:"):
                    deal_id = int(data.split(":", 1)[1])
                    self.answer_callback(cq["id"])
                    store.set_setting(f"edit_deal_id:{user_id}", str(deal_id))
                    self.send_bot_message(chat_id,
                        "Отправь одним сообщением новое название. Для цены, кода, текста и ссылки используй формат:\n"
                        "название | цена | старая цена | промокод | URL товара | описание")

                elif data == "show_card":
                    self.answer_callback(cq["id"])
                    self.send_bot_message(
                        chat_id,
                        "🃏 Готовая карточка: так выглядит пост в канале — "
                        "фото, название, цена было-стало, температура, промокод, "
                        "«Подробнее о скидке» и ссылка. Всё это бот собирает автоматически.",
                    )

                elif data == "show_data":
                    self.answer_callback(cq["id"])
                    self.send_bot_message(
                        chat_id,
                        "📄 Распознанные поля: 1) фото товара, 2) название, "
                        "3) цена (было → стало), 4) промокод, 5) «Подробнее о скидке». "
                        "Всё берётся со страницы сделки на pepper.ru — без браузера.",
                    )

        return new_offset
