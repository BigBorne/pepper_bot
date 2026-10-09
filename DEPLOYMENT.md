# Развертывание на сервере

## Проблема с капчей на сервере

Если локально бот работает, а на сервере pepper.ru отдает капчу (редирект на `/tmgrdfrend/showcaptcha`), это блокировка по IP — датацентр заблокирован.

### Решение 1: 2captcha API (рекомендуется)

1. **Получить API ключ:**
   - Регистрация: https://2captcha.com/enterpage
   - Пополнить баланс: ~$3 за 1000 капч
   - Скопировать API ключ из личного кабинета

2. **Добавить в `.env` на сервере:**
   ```bash
   PEPPER_2CAPTCHA_KEY=ваш_ключ_2captcha
   ```

3. **Перезапустить бота:**
   ```bash
   sudo systemctl restart pepper-bot
   sudo journalctl -u pepper-bot -f
   ```

Бот автоматически:
- Определит капчу по редиректу или заголовку `x-yandex-captcha`
- Извлечет `sitekey` из HTML страницы
- Отправит задачу на 2captcha
- Дождется решения (~5-30 секунд)
- Применит токен и продолжит работу

**Стоимость:** ~$0.003 за капчу, обычно требуется при первом запросе после смены IP.

### Решение 2: Residential прокси

Используй прокси с российских residential IP (они труднее детектятся):

```bash
# в .env
PEPPER_PROXY=http://user:pass@proxy.provider.com:port
```

Проверить прокси:
```bash
curl -x 'http://user:pass@proxy:port' -I https://www.pepper.ru/deals
```

Если возвращает `HTTP/2 200` без редиректа на `showcaptcha` — прокси работает.

### Решение 3: Запуск локально

Если у тебя стабильный домашний интернет:
1. Запускай бота на локальной машине
2. Держи компьютер включенным или используй Raspberry Pi / старый ноутбук
3. Настрой автозапуск через systemd/launchd

## Проверка на сервере

```bash
# Проверить что капча возникает
curl -I https://www.pepper.ru/deals

# Если видишь location: .../showcaptcha — нужно решение выше
# Если HTTP/2 200 — всё ок

# Проверить что 2captcha ключ работает
source .venv/bin/activate
python3 -c "
from captcha_solver import CaptchaSolver
solver = CaptchaSolver('твой_ключ')
print('API key valid' if solver.api_key else 'API key missing')
"
```

## Логи и диагностика

```bash
# Посмотреть что бот делает
sudo journalctl -u pepper-bot -f

# Если видишь "yandex captcha detected" — 2captcha работает
# Если видишь "captcha solved" — капча прошла успешно
# Если видишь "captcha solving failed" — проверь баланс на 2captcha.com
```

## Альтернатива: cookies из браузера

Если не хочешь платить за 2captcha:
1. Открой pepper.ru в браузере
2. Пройди капчу вручную
3. F12 → Console → `document.cookie`
4. Скопируй cookie `spravka` и добавь в код вручную

Но этот способ временный — cookie истечет через ~месяц.
