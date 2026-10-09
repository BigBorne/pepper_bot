# -*- coding: utf-8 -*-
"""2captcha integration for Yandex SmartCaptcha solving."""
from __future__ import annotations

import logging
import time

from curl_cffi.requests import Session

log = logging.getLogger("pepper.captcha")


class CaptchaSolver:
    """Решение Yandex SmartCaptcha через 2captcha API."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.session = Session()
        self.api_url = "https://api.2captcha.com"

    def solve_yandex_smart(self, sitekey: str, pageurl: str, max_wait: int = 120,
                           user_agent: str | None = None,
                           cookies: str | None = None) -> str | None:
        """Решить Yandex SmartCaptcha.

        Args:
            sitekey: ключ сайта из HTML
            pageurl: URL страницы с капчей
            max_wait: максимальное время ожидания решения (сек)

        Returns:
            токен решения или None при ошибке
        """
        task_id = self._submit_task(sitekey, pageurl, user_agent, cookies)
        if not task_id:
            return None

        # ждать решения
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            result = self._get_result(task_id)
            if result is not None:
                return result
            if time.time() - start > max_wait:
                log.error("captcha solving timeout after %d seconds", max_wait)
                return None

        return None

    def _submit_task(self, sitekey: str, pageurl: str,
                     user_agent: str | None, cookies: str | None) -> str | None:
        """Отправить задачу на решение."""
        task = {
            "type": "YandexSmartCaptchaTaskProxyless",
            "websiteURL": pageurl,
            "websiteKey": sitekey,
        }
        if user_agent:
            task["userAgent"] = user_agent
        if cookies:
            task["cookies"] = cookies
        try:
            r = self.session.post(
                f"{self.api_url}/createTask",
                json={"clientKey": self.api_key, "task": task},
                timeout=30,
            )
            data = r.json()
            if data.get("errorId") == 0 and data.get("taskId"):
                task_id = str(data["taskId"])
                log.info("captcha task submitted: %s", task_id)
                return task_id
            else:
                log.error("captcha task submission failed: %s", data.get("errorDescription") or data)
                return None
        except Exception as exc:
            log.error("captcha submission error: %s", exc)
            return None

    def _get_result(self, task_id: str) -> str | None:
        """Получить результат решения; None означает ещё не готов."""
        try:
            r = self.session.post(
                f"{self.api_url}/getTaskResult",
                json={"clientKey": self.api_key, "taskId": int(task_id)},
                timeout=30,
            )
            data = r.json()
            if data.get("errorId"):
                log.error("captcha solving failed: %s", data.get("errorDescription") or data)
                return None
            if data.get("status") == "ready":
                token = (data.get("solution") or {}).get("token")
                if token:
                    log.info("captcha solved")
                return token
            return None  # processing
        except Exception as exc:
            log.error("captcha result check error: %s", exc)
            return None


def extract_sitekey_from_captcha_page(html: str, page_url: str) -> str | None:
    """Извлечь sitekey из страницы капчи Yandex SmartCaptcha.

    Yandex SmartCaptcha обычно содержит скрипт вида:
    <script src="https://smartcaptcha.yandexcloud.net/captcha.js?render=onload&sitekey=..."></script>
    или параметр data-sitekey в div, или внутри JS кода window.smartCaptcha
    """
    import re

    # 1. Попробовать найти в script src с sitekey параметром
    match = re.search(r'[?&]sitekey=([A-Za-z0-9_-]+)', html)
    if match:
        log.info("sitekey found in script src: %s", match.group(1))
        return match.group(1)

    # 2. Попробовать найти в data-sitekey атрибуте
    match = re.search(r'data-sitekey=["\']([A-Za-z0-9_-]+)["\']', html, re.IGNORECASE)
    if match:
        log.info("sitekey found in data-sitekey: %s", match.group(1))
        return match.group(1)

    # 3. Попробовать найти внутри JS переменной (window.smartCaptcha, captchaSettings и т.д.)
    match = re.search(r'["\']sitekey["\']\s*:\s*["\']([A-Za-z0-9_-]+)["\']', html)
    if match:
        log.info("sitekey found in JS variable: %s", match.group(1))
        return match.group(1)

    # 4. Более широкий паттерн для любого упоминания sitekey
    match = re.search(r'sitekey["\']?\s*[:=]\s*["\']([A-Za-z0-9_-]{20,})["\']', html, re.IGNORECASE)
    if match:
        log.info("sitekey found with broad pattern: %s", match.group(1))
        return match.group(1)

    log.error("sitekey not found in captcha page, saved HTML has %d chars", len(html))
    return None
