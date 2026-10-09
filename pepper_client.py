# -*- coding: utf-8 -*-
"""Клиент pepper.ru.

Особенность сайта: перед выдачей контента Yandex-антибот (ycch) отдаёт
страницу «Верификация» с embedded-блоком SSR_DATA (base64 JSON), в котором:

  * action — URL обработчика /tmgrdfrend/checkcaptchafast?... (retpath внутри);
  * pow.prefix — 64 hex-символа;
  * pow.complexity — требуемое число ведущих нулевых бит sha256(prefix + nonce).

Решение: брутфорсим nonce (сложность 10 бит => в среднем ~1024 итерации,
доли миллисекунды), POST-им {"pow": "<nonce>"} на action — в ответе прилетает
настоящая страница, а сессия получает cookies, после чего обычные GET проходят
напрямую. Никакого браузера не нужно: TLS-отпечаток имитируется через
curl_cffi (impersonate="chrome").

Парсинг: ленты (/deals, /new) — серверный рендер, карточки div с классом
"md:card". Страница сделки — JSON-LD (Article / Product / WebPage) плюс
тело описания в div.content-formatting и температура в виджете голосования.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

from curl_cffi.requests import Session
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from captcha_solver import CaptchaSolver, extract_sitekey_from_captcha_page

log = logging.getLogger("pepper.client")

BASE = "https://www.pepper.ru"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SSR_DATA_RE = re.compile(r'__SSR_DATA__=JSON\.parse\(atob\("([^"]+)"\)\)')
_DEAL_ID_RE = re.compile(r"/deals/[^\"'#?]+-(\d{5,9})")
_TEMP_RE = re.compile(r"(\d[\d\s]*)\s*°")
_PRICE_RE = re.compile(r"(\d[\d\s]{0,11})\s*₽")
_CHALLENGE_MARK = "tmgrdfrend"

# Промокод в DOM: span с кодом перед ссылкой /visit/deals (виджет копирования)
_CODE_DOM_RE = re.compile(
    r"<span[^>]*>([^<]{2,30})</span>\s*<a[^>]*href=\"https?://www\.pepper\.ru/visit/deals"
)
# Промокод в тексте описания: «Промокод: XXXX», «код XXXX» и т.п.
_CODE_RE = re.compile(
    r"(?:промокод|кодовое\s+слово|(?:^|\s)код)\s*"
    r"(?:на\s+\S+\s*)?[:\-–—\s]*\s*([A-Z0-9]{3,20}|[A-Z0-9\-]{4,24})\b",
    re.IGNORECASE,
)
# Голый капс-токен в теле (например WBCLUB45) — эвристика с приоритетом ниже.
_BARE_CODE_RE = re.compile(r"\b([A-Z]{2,5}\d{2,6}|\d{2,6}[A-Z]{2,5}|[A-Z0-9]{6,12})\b")

# Комментарии: иконка svg#comment, рядом число
_COMMENTS_RE = re.compile(r"svg#comment[^<]*</use>\s*</svg>\s*(\d{1,7})")

_WS_RE = re.compile(r"\s+")


class PepperBlocked(RuntimeError):
    """Не удалось пройти верификацию / страница не распознана."""


@dataclass
class DealCard:
    """Минимум из ленты — достаточно для обнаружения новой сделки."""

    deal_id: int
    url: str
    title: str = ""
    temperature: int | None = None
    price: float | None = None
    old_price: float | None = None
    image: str | None = None
    expired: bool = False


@dataclass
class Deal(DealCard):
    """Полная карточка со страницы сделки."""

    description: str = ""
    body_text: str = ""
    promo_code: str | None = None
    merchant: str | None = None
    published_at: str | None = None
    expires_at: str | None = None
    available: bool = True
    comments: int | None = None
    product_url: str | None = None
    product_visit_url: str | None = None
    all_images: list[str] | None = None


def _num(value) -> float | None:
    try:
        f = float(str(value).replace(" ", "").replace(",", "."))
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def _clean(text: str) -> str:
    # нулевые символы, NBSP-мусор и лишние пробелы
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u00ad", "")
    return _WS_RE.sub(" ", text).strip()


class PepperClient:
    def __init__(
        self,
        request_delay: float = 2.0,
        timeout: float = 60.0,
        max_challenge_rounds: int = 4,
        proxy: str | None = None,
        captcha_api_key: str | None = None,
    ) -> None:
        self.session = Session(impersonate="chrome124")
        if proxy:
            self.session.proxies = {"https://": proxy, "http://": proxy}
            log.info("using proxy: %s", proxy)
        self.session.headers.update({
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Referer": "https://www.pepper.ru/",
        })
        self.request_delay = request_delay
        self.timeout = timeout
        self.max_challenge_rounds = max_challenge_rounds
        self._last_request_ts = 0.0
        self.captcha_solver = CaptchaSolver(captcha_api_key) if captcha_api_key else None

    # ------------------------------------------------------------------ #
    # Сеть
    # ------------------------------------------------------------------ #

    def _throttle(self) -> None:
        wait = self.request_delay - (time.monotonic() - self._last_request_ts)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.5))
        self._last_request_ts = time.monotonic()

    def _headers(self, referer: str | None = None) -> dict:
        h = {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        if referer:
            h["Referer"] = referer
        return h

    def get(self, url: str) -> str:
        """GET с автоматическим прохождением ycch-челленджа."""
        for _ in range(self.max_challenge_rounds):
            self._throttle()
            r = self.session.get(url, headers=self._headers(), timeout=self.timeout)

            # проверка на Yandex SmartCaptcha (редирект на showcaptcha)
            if "showcaptcha" in r.url or "x-yandex-captcha" in r.headers:
                if self.captcha_solver:
                    log.info("yandex captcha detected, solving via 2captcha")
                    if self._solve_yandex_captcha(r.text, r.url):
                        # после решения капчи повторяем запрос
                        continue
                else:
                    log.warning("yandex captcha detected but no 2captcha key configured")
                    raise PepperBlocked(f"captcha required: {url}")

            if _CHALLENGE_MARK not in r.text:
                return r.text
            html = self._solve_challenge(r.text, referer=url)
            if _CHALLENGE_MARK not in html:
                return html
        raise PepperBlocked(f"challenge not passed after {self.max_challenge_rounds} rounds: {url}")

    def _solve_challenge(self, challenge_html: str, referer: str) -> str:
        m = _SSR_DATA_RE.search(challenge_html)
        if not m:
            raise PepperBlocked("challenge page without SSR_DATA block")
        ssr = json.loads(base64.b64decode(m.group(1)))

        action: str = ssr["action"]
        if action.startswith("/"):
            action = BASE + action

        prefix: str = ssr["pow"]["prefix"]
        bits: int = int(ssr["pow"]["complexity"])
        target = (1 << (256 - bits)) - 1  # sha256 <= target <=> `bits` ведущих нулей

        t0 = time.monotonic()
        nonce = 0
        while int.from_bytes(hashlib.sha256(f"{prefix}{nonce}".encode()).digest(), "big") > target:
            nonce += 1
        log.debug("PoW solved: nonce=%d in %.1f ms", nonce, (time.monotonic() - t0) * 1000)

        self._throttle()
        r = self.session.post(
            action,
            headers={**self._headers(referer=referer), "Origin": BASE},
            data={"pow": str(nonce)},
            timeout=self.timeout,
        )
        return r.text

    def _solve_yandex_captcha(self, html: str, page_url: str) -> bool:
        """Решить Yandex SmartCaptcha через 2captcha."""
        # сохранить HTML для отладки
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, dir='/tmp') as f:
            f.write(html)
            log.info("captcha page saved to %s", f.name)

        sitekey = extract_sitekey_from_captcha_page(html, page_url)
        if not sitekey:
            log.error("failed to extract sitekey from captcha page")
            return False

        log.info("extracted sitekey: %s", sitekey)
        token = self.captcha_solver.solve_yandex_smart(sitekey, BASE)
        if not token:
            log.error("failed to solve captcha")
            return False

        # отправить токен обратно на pepper.ru
        # обычно это форма или AJAX запрос с токеном
        # для Yandex SmartCaptcha нужно отправить smart-token в форму или cookie
        try:
            # pepper.ru использует cookie spravka после решения капчи
            # попробуем установить токен как cookie и повторить запрос
            self.session.cookies.set("smart-token", token, domain=".pepper.ru")
            log.info("captcha token set, retrying request")
            return True
        except Exception as exc:
            log.error("failed to apply captcha token: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Ленты
    # ------------------------------------------------------------------ #

    def feed_cards(self, feeds: Iterable[str], max_pages: int = 1) -> list[DealCard]:
        cards: dict[int, DealCard] = {}
        for feed in feeds:
            page_url = feed
            for page in range(max(1, max_pages)):
                try:
                    html = self.get(page_url)
                except Exception as exc:  # одна лента упала — тянем остальные
                    log.warning("feed %s page %s failed: %s", feed, page + 1, exc)
                    break
                tree = HTMLParser(html)
                for node in tree.css("article"):
                    cls = node.attributes.get("class") or ""
                    if "deal-card" not in cls:
                        continue
                    card = self._parse_card(node)
                    if card is not None:
                        cards.setdefault(card.deal_id, card)
                next_link = tree.css_first("a[rel='next']")
                if next_link is None:
                    next_link = tree.css_first("a[href*='page='][aria-label*='next'], a[href*='page=']")
                if next_link is None or not next_link.attributes.get("href"):
                    break
                page_url = urljoin(page_url, next_link.attributes["href"])
        return sorted(cards.values(), key=lambda c: c.deal_id)

    def _parse_card(self, node) -> DealCard | None:
        a = node.css_first("a[href*='/deals/']")
        if a is None:
            return None
        href = (a.attributes.get("href") or "").split("#")[0].split("?")[0]
        m = _DEAL_ID_RE.search(href)
        if not m:
            return None
        url = urljoin(BASE + "/", href)

        title = _clean(a.text(strip=True))
        if not title:
            t = node.css_first("[class*='card-title'], h2")
            title = _clean(t.text(strip=True)) if t else ""

        text = _clean(node.text(separator=" ", strip=True))

        temp = None
        tm = _TEMP_RE.search(text)
        if tm:
            temp = int(tm.group(1).replace(" ", "") or 0)

        price = old_price = None
        prices = [_num(p) for p in _PRICE_RE.findall(text)]
        prices = [p for p in prices if p]
        if prices:
            price = prices[0]
            if len(prices) > 1 and prices[1] > prices[0]:
                old_price = prices[1]

        image = None
        img = node.css_first("img")
        if img is not None:
            image = img.attributes.get("src") or img.attributes.get("data-src")
            if image:
                image = urljoin(BASE + "/", image)

        expired = bool(re.search(r"истекл|срок.*законч|expired", text, re.IGNORECASE))

        return DealCard(
            deal_id=int(m.group(1)),
            url=url,
            title=title,
            temperature=temp,
            price=price,
            old_price=old_price,
            image=image,
            expired=expired,
        )

    # ------------------------------------------------------------------ #
    # Страница сделки
    # ------------------------------------------------------------------ #

    def get_deal(self, url: str, card: DealCard | None = None,
                 resolve_product: bool = True) -> Deal:
        deal_id_m = _DEAL_ID_RE.search(urlparse(url).path)
        if not deal_id_m:
            raise ValueError(f"not a deal url: {url}")
        deal = Deal(deal_id=int(deal_id_m.group(1)), url=url)
        if card is not None:
            deal.title = card.title
            deal.temperature = card.temperature
            deal.price = card.price
            deal.old_price = card.old_price
            deal.image = card.image
            deal.expired = card.expired

        html = self.get(url)
        tree = HTMLParser(html)

        # Соберём все кандидаты для картинок
        image_candidates: list[str] = []

        # --- JSON-LD: Article / Product / WebPage ---------------------- #
        for script in tree.css("script[type='application/ld+json']"):
            try:
                data = json.loads(script.text())
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            jtype = data.get("@type")
            if jtype == "Article":
                deal.title = _clean(data.get("headline") or deal.title)
                deal.published_at = data.get("datePublished") or deal.published_at
                imgs = data.get("image")
                if isinstance(imgs, dict) and imgs.get("url"):
                    image_candidates.append(imgs["url"])
                elif isinstance(imgs, list):
                    image_candidates.extend([i for i in imgs if isinstance(i, str)])
                elif isinstance(imgs, str):
                    image_candidates.append(imgs)
            elif jtype == "Product":
                deal.title = deal.title or _clean(data.get("name") or "")
                offers = data.get("offers") or {}
                p = _num(offers.get("price"))
                if p is not None:
                    deal.price = p
                deal.expires_at = offers.get("priceValidUntil") or deal.expires_at
                availability = str(offers.get("availability") or "")
                deal.available = availability.endswith("InStock")
                brand = data.get("brand") or {}
                if isinstance(brand, dict) and brand.get("name"):
                    deal.merchant = _clean(brand["name"])
                imgs = data.get("image")
                if isinstance(imgs, list):
                    image_candidates.extend([i for i in imgs if isinstance(i, str)])
                elif isinstance(imgs, str):
                    image_candidates.append(imgs)
            elif jtype == "WebPage":
                deal.description = _clean(data.get("description") or "")

        # og:image и meta property="image"
        for meta in tree.css("meta[property='og:image'], meta[property='image'], meta[name='twitter:image']"):
            content = meta.attributes.get("content")
            if content:
                image_candidates.append(content)

        # <img> внутри content-formatting (инфографики, скриншоты)
        body = tree.css_first("div.content-formatting")
        if body is not None:
            for img in body.css("img"):
                src = img.attributes.get("src") or img.attributes.get("data-src")
                if src:
                    image_candidates.append(src)

        # Картинка из карточки в ленте (фоллбэк)
        if card and card.image:
            image_candidates.append(card.image)

        # Абсолютизируем URL
        image_candidates = [urljoin(BASE + "/", u) for u in image_candidates if u]
        # Убираем дубли
        seen_urls = set()
        unique_images = []
        for u in image_candidates:
            if u not in seen_urls:
                seen_urls.add(u)
                unique_images.append(u)

        deal.all_images = unique_images if unique_images else None

        # Выбираем самую большую картинку из доступных
        if deal.all_images:
            deal.image = self._pick_best_image(deal.all_images)
        elif card and card.image:
            deal.image = card.image

        # --- тело описания --------------------------------------------- #
        body = tree.css_first("div.content-formatting")
        if body is not None:
            deal.body_text = _clean(body.text(separator=" ", strip=True))

        # Выбираем лучшую картинку по эвристике размера URL
        if deal.all_images:
            deal.image = self._pick_best_image(deal.all_images)
        elif not deal.image and card and card.image:
            deal.image = card.image

        # Ссылка Pepper /visit/deals редиректит на магазин. Не публикуем URL
        # Pepper в карточке: извлекаем конечный адрес перехода.
        visit_link = tree.css_first("a[href*='/visit/deals-'][href*='buy-now']")
        if visit_link is None:
            visit_link = tree.css_first("a[href*='/visit/deals-']")
        if visit_link is not None:
            href = visit_link.attributes.get("href", "")
            if href:
                deal.product_visit_url = urljoin(BASE + "/", href)
                if resolve_product:
                    deal.product_url = self.resolve_product_url(deal.product_visit_url, referer=url)

        # URL магазина иногда уже есть у внешней ссылки внутри описания.
        if not deal.product_url and body is not None:
            for link in body.css("a[href]"):
                href = link.attributes.get("href", "")
                host = urlparse(href).hostname or ""
                if href.startswith(("http://", "https://")) and host and not host.endswith("pepper.ru"):
                    deal.product_url = href
                    break

        # Цена до скидки: берём только зачёркнутый элемент на странице сделки,
        # а не произвольное число из описания/боковой ленты.
        if deal.old_price is None:
            old_node = tree.css_first(".line-through")
            if old_node is not None:
                old_match = re.search(r"\d[\d\s]*", old_node.text(strip=True))
                if old_match:
                    deal.old_price = _num(old_match.group(0))

        # --- температура ------------------------------------------------ #
        if deal.temperature is None:
            tm = _TEMP_RE.search(html)
            if tm:
                deal.temperature = int(tm.group(1).replace(" ", "") or 0)

        # --- комментарии ------------------------------------------------- #
        cm = _COMMENTS_RE.search(html)
        if cm:
            deal.comments = int(cm.group(1))

        # --- промокод ---------------------------------------------------- #
        # 1) виджет копирования в DOM (точный источник)
        m = _CODE_DOM_RE.search(html)
        if m:
            deal.promo_code = m.group(1).strip()
        # 2) текст описания
        if not deal.promo_code:
            deal.promo_code = (
                self.extract_code(deal.body_text) or self.extract_code(deal.description)
            )
        return deal

    def scan_recent(self, feeds: Iterable[str], hours: int,
                    max_items: int = 50) -> list[Deal]:
        """Живой скан сделок, опубликованных на Pepper за последние N часов.

        Время берётся из Article.datePublished на самой странице сделки.
        Поэтому кнопка периода не зависит от того, успел ли основной цикл
        сохранить сделку в SQLite. Скан возвращает и скидки, и промокоды.
        """
        if hours not in (3, 12, 24):
            raise ValueError("hours must be one of 3, 12, 24")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        # 24 ч требуют прохода по страницам ленты: новые сделки идут примерно
        # по 20 на страницу. Проверяем больше страниц для трёхчасового окна тоже,
        # потому что лента может содержать рекламные/закреплённые карточки.
        pages = {3: 3, 12: 10, 24: 20}[hours]
        cards = self.feed_cards(feeds, max_pages=pages)
        deals: list[Deal] = []
        seen: set[int] = set()

        # На текущих лентах Pepper обычно достаточно одной страницы. Берём
        # самые свежие по ID первыми, чтобы результат начинался с новых.
        cards.sort(key=lambda card: card.deal_id, reverse=True)
        for card in cards:
            if card.deal_id in seen:
                continue
            seen.add(card.deal_id)
            try:
                deal = self.get_deal(card.url, card, resolve_product=False)
            except Exception as exc:
                log.warning("recent scan: deal %s failed: %s", card.deal_id, exc)
                continue

            published = self._published_datetime(deal.published_at)
            if published is None:
                log.debug("recent scan: deal %s has no datePublished", deal.deal_id)
                continue
            if published >= cutoff:
                if deal.product_visit_url:
                    deal.product_url = self.resolve_product_url(deal.product_visit_url, referer=deal.url)
                deals.append(deal)
            # Ленты не гарантируют сортировку по времени, поэтому здесь не
            # останавливаемся на первой старой карточке.
            if len(deals) >= max_items:
                break

        deals.sort(key=lambda deal: self._published_datetime(deal.published_at)
                   or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return deals

    def scan_recent_progress(self, feeds: Iterable[str], hours: int,
                             max_items: int = 50, max_cards: int = 250,
                             progress=None) -> list[Deal]:
        """То же, но сообщает прогресс и ограничивает число страниц сделок.

        `progress(done, total, found)` вызывается после обработки каждой сделки.
        """
        if hours not in (3, 12, 24):
            raise ValueError("hours must be one of 3, 12, 24")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        pages = {3: 3, 12: 10, 24: 20}[hours]
        cards = self.feed_cards(feeds, max_pages=pages)
        cards.sort(key=lambda card: card.deal_id, reverse=True)
        cards = cards[:max_cards]
        found: list[Deal] = []
        seen: set[int] = set()
        for index, card in enumerate(cards, 1):
            if card.deal_id not in seen:
                seen.add(card.deal_id)
                try:
                    deal = self.get_deal(card.url, card, resolve_product=False)
                    published = self._published_datetime(deal.published_at)
                    if published is not None and published >= cutoff:
                        if deal.product_visit_url:
                            deal.product_url = self.resolve_product_url(deal.product_visit_url, referer=deal.url)
                        found.append(deal)
                except Exception as exc:
                    log.warning("recent scan: deal %s failed: %s", card.deal_id, exc)
            if progress:
                progress(index, len(cards), len(found))
            if len(found) >= max_items:
                break
        found.sort(key=lambda deal: self._published_datetime(deal.published_at)
                   or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return found

    @staticmethod
    def _published_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def resolve_product_url(self, visit_url: str, referer: str = BASE) -> str | None:
        """Разрешает Pepper clickout и возвращает целевой магазинский URL."""
        try:
            for _ in range(self.max_challenge_rounds):
                self._throttle()
                response = self.session.get(
                    visit_url,
                    headers=self._headers(referer=referer),
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                if _CHALLENGE_MARK in response.text:
                    solved = self._solve_challenge(response.text, referer=visit_url)
                    if _CHALLENGE_MARK in solved:
                        continue
                    # Повторить clickout уже с проставленной challenge-cookie.
                    continue

                resolved = response.url
                target = parse_qs(urlparse(resolved).query).get("url", [None])[0]
                if target and urlparse(target).scheme in ("http", "https"):
                    return target
                host = urlparse(resolved).hostname or ""
                if host and not host.endswith("pepper.ru"):
                    return resolved

                # Affiliate-ссылка иногда возвращает URL цели в query string,
                # даже если финальный URL остался на домене трекера.
                for key in ("url", "target", "to", "redirect"):
                    target = parse_qs(urlparse(resolved).query).get(key, [None])[0]
                    if target and urlparse(target).scheme in ("http", "https"):
                        return target
        except Exception as exc:
            log.warning("could not resolve product URL %s: %s", visit_url, exc)
        return None

    @staticmethod
    def extract_code(text: str) -> str | None:
        if not text:
            return None
        m = _CODE_RE.search(text)
        if m:
            code = m.group(1).strip(" .,-–—")
            if len(code) >= 3:
                return code
        # эвристика: капс-токен рядом со словом «код/промокод» в радиусе 60 символов
        for m in re.finditer(r"(?iu)(промокод|код)", text):
            window = text[m.end(): m.end() + 60]
            bm = _BARE_CODE_RE.search(window)
            if bm:
                return bm.group(1)
        return None

    def _pick_best_image(self, urls: list[str]) -> str | None:
        """Выбирает самую большую картинку из списка по эвристике размера URL."""
        if not urls:
            return None
        
        # Приоритет 1: ищем маркеры высокого разрешения в URL
        for marker in ("1200", "1024", "800", "large", "original", "full"):
            for url in urls:
                if marker in url.lower():
                    return url
        
        # Приоритет 2: максимальная длина URL (часто коррелирует с параметрами размера)
        return max(urls, key=len)

    # ------------------------------------------------------------------ #
    # Медиа
    # ------------------------------------------------------------------ #

    def download_image(self, url: str | None, max_bytes: int = 6 * 1024 * 1024) -> bytes | None:
        """Скачать картинку для sendPhoto. None => постим без фото."""
        if not url:
            return None
        try:
            self._throttle()
            r = self.session.get(url, headers=self._headers(referer=BASE), timeout=self.timeout)
            ctype = r.headers.get("content-type", "")
            if r.status_code != 200 or not ctype.startswith("image"):
                log.warning("image fetch got %s (%s)", r.status_code, ctype or "no ctype")
                return None
            data = r.content[:max_bytes]
            return data or None
        except Exception as exc:
            log.warning("image download failed: %s", exc)
            return None

    def close(self) -> None:
        self.session.close()
