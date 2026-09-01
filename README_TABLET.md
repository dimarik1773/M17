# Training Telegram App — Tablet Edition

Эта версия специально упрощена для развёртывания с планшета:
- один FastAPI web service;
- frontend находится в `index.html` и отдаётся тем же сервером;
- Telegram работает через webhook, отдельный background worker не нужен;
- API и первая логика тренировочных планов находятся в `main.py`.

## Переменные окружения
- `TELEGRAM_BOT_TOKEN` — токен от BotFather. Не публиковать в GitHub.
- `PUBLIC_URL` — HTTPS адрес сервиса после первого деплоя, например `https://training-app-abc.onrender.com`.
- `TELEGRAM_WEBHOOK_SECRET` — случайная строка из букв/цифр/`_`/`-`.

## Проверка
- `/health` должен вернуть `{"status":"ok",...}`.
- После настройки переменных отправьте боту `/start`.
