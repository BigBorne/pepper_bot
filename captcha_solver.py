# -*- coding: utf-8 -*-
"""2captcha integration for Yandex SmartCaptcha solving."""
from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

from curl_cffi.requests import Session

log = logging.getLogger("pepper.captcha")


class CaptchaSolver:
    """Решение Yandex SmartCaptcha через 2captcha API."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.session = Session()
        self.in_url = "https://2captcha.com/in.php"
        self.res_url = "https://2captcha.com/res.php"

    def solve_yandex_smart(self, sitekey: str, pageurl: str, max_wait: int = 120) -> str | None:
        """Решить Yandex SmartCaptcha.

        Args:
            sitekey: ключ сайта из HTML
            pageurl: URL страницы с капчей
            max_wait: максимальное время ожидания решения (сек)

        Returns:
            токен решения или None при ошибке
        """
        # отправить задачу
        task_id = self._submit_task(sitekey, pageurl)
        if not task_id:
            return None

        # ждать решения
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            token = self._get_result(task_id)
            if token:
                return token
            if time.time() - start > max_wait:
                log.error("captcha solving timeout after %d seconds", max_wait)
                return None

        return None

    def _submit_task(self, sitekey: str, pageurl: str) -> str | None:
        """Отправить задачу на решение."""
        params = {
            "key": self.api_key,
            "method": "yandex",
            "sitekey": sitekey,
            "pageurl": pageurl,
            "json": "1",
        }
        try:
            r = self.session.post(self.in_url, data=params, timeout=30)
            data = r.json()
            if data.get("status") == 1:
                task_id = data.get("request")
                log.info("captcha task submitted: %s", task_id)
                return task_id
            else:
                log.error("captcha task submission failed: %s", data.get("request"))
                return None
        except Exception as exc:
            log.error("captcha submission error: %s", exc)
            return None

    def _get_result(self, task_id: str) -> str | None:
        """Получить результат решения."""
        params = {
            "key": self.api_key,
            "action": "get",
            "id": task_id,
            "json": "1",
        }
        try:
            r = self.session.get(self.res_url, params=params, timeout=30)
            data = r.json()
            status = data.get("status")
            if status == 1:
                token = data.get("request")
                log.info("captcha solved: %s", token[:50])
                return token
            elif data.get("request") == "CAPCHA_NOT_READY":
                return None  # еще не готово
            else:
                log.error("captcha solving failed: %s", data.get("request"))
                return None
        except Exception as exc:
            log.error("captcha result check error: %s", exc)
            return None


def extract_sitekey_from_captcha_page(html: str, page_url: str) -> str | None:
    """Извлечь sitekey из страницы капчи Yandex SmartCaptcha.

    Yandex SmartCaptcha обычно содержит скрипт вида:
    <script src="https://smartcaptcha.yandexcloud.net/captcha.js?render=onload&sitekey=..."></script>
    или параметр data-sitekey в div
    """
    import re

    # попробовать найти в script src
    match = re.search(r'[?&]sitekey=([A-Za-z0-9_-]+)', html)
    if match:
        return match.group(1)

    # попробовать найти в data-sitekey
    match = re.search(r'data-sitekey=["\']([A-Za-z0-9_-]+)["\']', html)
    if match:
        return match.group(1)

    log.error("sitekey not found in captcha page")
    return None
