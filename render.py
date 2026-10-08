# -*- coding: utf-8 -*-
"""Рендер готовых Telegram-карточек для скидок и промокодов."""
from __future__ import annotations

import html
import re
from datetime import datetime

from pepper_client import Deal

PHOTO_CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def fmt_price(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".")
    return f"{int(value):,}".replace(",", " ")


def _fmt_expires(raw: str | None) -> str | None:
    if not raw:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if not match:
        return None
    y, month, day = map(int, match.groups())
    try:
        date = datetime(y, month, day).date()
    except ValueError:
        return None
    if date < datetime.now().date():
        return None
    return date.strftime("%d.%m.%Y")


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _deal_link(deal: Deal, label: str = "Ссылка на товар") -> str:
    if not deal.product_url:
        return ""
    return f'<a href="{_escape(deal.product_url)}">{_escape(label)}</a>'


def _conditions(deal: Deal) -> list[str]:
    """Только известные/типовые оговорки, не сочиняем условия сделки."""
    text = (deal.body_text + " " + deal.description).lower()
    conditions: list[str] = []
    if re.search(r"онлайн|online|оплат[аы].{0,20}сайт", text):
        conditions.append("цена может зависеть от способа оплаты;")
    if re.search(r"регион|город|доставк[аи].{0,30}город", text):
        conditions.append("цена или наличие могут отличаться по регионам;")
    if re.search(r"новых пользователей|новым пользовател|нет действующ|не было подписк", text):
        conditions.append("проверьте ограничения для аккаунта перед активацией;")
    if re.search(r"количеств.{0,25}огранич|пока есть в наличии|остатк", text):
        conditions.append("количество товара может быть ограничено;")
    return conditions


def _expiry_line(deal: Deal, promo: bool) -> str | None:
    expiry = _fmt_expires(deal.expires_at)
    if not expiry:
        return None
    if promo:
        try:
            date = datetime.strptime(expiry, "%d.%m.%Y").date()
            days = (date - datetime.now().date()).days
        except ValueError:
            return f"⏳ Действует до {expiry}"
        return f"⏳ Действует ещё {days} дн." if days >= 0 else None
    return f"акция действует до {expiry}."


def _saving(deal: Deal) -> int | None:
    if deal.price is None or deal.old_price is None or deal.old_price <= deal.price:
        return None
    try:
        return int(deal.old_price - deal.price)
    except (TypeError, ValueError):
        return None


def _rating(deal: Deal) -> tuple[str, str]:
    """Редакционная заглушка-консервативная оценка; не выводим из температуры Pepper."""
    if deal.promo_code:
        return "9/10", "Хорошее предложение, если подходит вашему аккаунту."
    saving = _saving(deal)
    if saving and deal.old_price:
        ratio = saving / deal.old_price
        score = "8,5/10" if ratio >= 0.25 else "8/10"
        verdict = "Можно рассмотреть, если цена и условия подходят."
        return score, verdict
    return "7/10", "Сравните цену и условия перед покупкой."


def render(deal: Deal) -> str:
    """Карточка с промокодом или скидкой, в пользовательском формате."""
    title = _escape(deal.title or "Предложение")
    body = (deal.body_text or deal.description or "").strip()
    link = _deal_link(deal)
    rating, verdict = _rating(deal)

    if deal.promo_code:
        lines = [
            "🐾 <b>Кот нашёл промокод</b>",
            f"🎁 <b>{title}</b>",
        ]
        # Короткий текст условий из описания оставляем отдельной строкой.
        if body:
            summary = body[:260].rstrip()
            if len(body) > 260:
                summary += "…"
            lines.append(_escape(summary))
        lines.append(f"🎟 Промокод — <code>{_escape(deal.promo_code)}</code>")
        expiry = _expiry_line(deal, promo=True)
        if expiry:
            lines.append(expiry)
        lines.extend([
            "Как получить:",
            "• перейти по ссылке;",
            "• ввести промокод;",
            "• проверить доступный срок и ограничения аккаунта перед активацией.",
            f"👉 Активировать промокод: {_deal_link(deal, 'Активировать промокод')}" if link else "",
            f"😼 Вердикт кота — {rating}",
            verdict,
            "🐾 Условия промокода могут отличаться для разных аккаунтов.",
        ])
        return "\n".join(lines)

    lines = [
        "🐾 <b>Кот нашёл скидку</b>",
        f"💻 <b>{title}</b>",
    ]
    if deal.price is not None:
        price = f"🔥 {fmt_price(deal.price)} ₽"
        if deal.old_price is not None and deal.old_price > deal.price:
            price += f" вместо {fmt_price(deal.old_price)} ₽"
        lines.append(price)
        saving = _saving(deal)
        if saving is not None:
            lines.append(f"💰 Экономия — {fmt_price(saving)} ₽")

    conditions = _conditions(deal)
    expiry = _expiry_line(deal, promo=False)
    if expiry:
        conditions.append(expiry)
    # Не приписываем универсальные условия, если страница их не подтверждает.
    if conditions:
        lines.append("")
        lines.append("Условия:")
        lines.extend(f"• {condition}" for condition in conditions)

    if link:
        lines.append(f"👉 Забрать по скидке: {link}")
    else:
        lines.append("👉 Ссылка на товар пока недоступна")
    lines.append(f"😼 Вердикт кота — {rating}")
    lines.append(verdict)
    lines.append("🐾 Цена актуальна на момент публикации.")
    return "\n".join(lines)


def render_promo_list(rows, hours: float) -> str:
    """Список промокодов за период (выдача бота по кнопке)."""
    if not rows:
        return f"🎟 Промокодов за последние {int(hours)} ч не найдено.\nПопробуй период побольше или дождись следующего сканирования."
    lines = [f"🎟 <b>Промокоды за последние {int(hours)} ч</b> ({len(rows)}):", ""]
    for row in rows:
        title = _escape((row["title"] or "")[:90])
        code = _escape(row["code"])
        lines.append(f"• <code>{code}</code>")
        lines.append(f"  {title}")
        if row["price"] is not None:
            lines.append(f"  Цена: {fmt_price(row['price'])} ₽")
        target = row.get("product_url") or row.get("url")
        if target:
            lines.append(f"  👉 <a href=\"{_escape(target)}\">Открыть товар</a>")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_details(title: str, price_line: str, promo_code: str | None, body: str) -> str:
    lines = ["🐾 <b>Кот нашёл промокод</b>", f"🎁 <b>{_escape(title)}</b>"]
    if price_line:
        lines.append(price_line)
    if promo_code:
        lines.append(f"🎟 Промокод — <code>{_escape(promo_code)}</code>")
    if body:
        lines.append(_escape(body))
    return "\n".join(lines)


def fit(text: str, limit: int) -> str:
    """Обрезка с сохранением баланса поддерживаемых HTML-тегов Telegram."""
    if len(text) <= limit:
        return text
    suffix = "…"
    budget = limit - len(suffix)
    # Сегментируем Telegram HTML-теги. Резервируем длину закрывающих тегов,
    # чтобы даже обрезка внутри длинного <a href="..."> оставалась валидной.
    tokens = re.findall(r"<[^>]+>|[^<]+", text)
    output: list[str] = []
    open_tags: list[str] = []
    used = 0
    closing = {"b": "</b>", "strong": "</strong>", "i": "</i>",
               "em": "</em>", "u": "</u>", "s": "</s>",
               "strike": "</strike>", "del": "</del>", "code": "</code>",
               "pre": "</pre>", "a": "</a>"}
    for token in tokens:
        if not token.startswith("<"):
            remaining = budget - used - sum(len(closing[tag]) for tag in open_tags)
            if len(token) > remaining:
                if remaining > 0:
                    output.append(token[:remaining])
                    used += remaining
                break
            output.append(token)
            used += len(token)
            continue
        closing_match = re.fullmatch(r"</([a-zA-Z]+)\s*>", token)
        if closing_match:
            name = closing_match.group(1).lower()
            if name in open_tags:
                # Вложенные теги закрываем до matching-tag, сохраняя валидность.
                while open_tags:
                    open_name = open_tags.pop()
                    close = closing[open_name]
                    output.append(close)
                    used += len(close)
                    if open_name == name:
                        break
            else:
                output.append(token)
                used += len(token)
            continue
        opening_match = re.match(r"<([a-zA-Z]+)(?:\s[^>]*)?>$", token)
        if opening_match and opening_match.group(1).lower() in closing:
            name = opening_match.group(1).lower()
            reserve = sum(len(closing[tag]) for tag in open_tags) + len(closing[name])
            if used + len(token) + reserve > budget:
                break
            output.append(token)
            used += len(token)
            open_tags.append(name)
            continue
        if used + len(token) > budget:
            break
        output.append(token)
        used += len(token)
    for tag in reversed(open_tags):
        output.append(closing[tag])
    return "".join(output).rstrip() + suffix
