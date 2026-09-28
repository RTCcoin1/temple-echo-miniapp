# Temple Echo: Jungle Ruins — Telegram Mini App

Готовая Telegram-совместимая игра `index.html` и бот-лаунчер на aiogram 3. Игра запускается как Web App из inline-кнопки в `/start` и из меню бота.

**Игра:** https://rtccoin1.github.io/temple-echo-miniapp/

Текущий режим — одиночное офлайн-выживание на арене храма: переживайте волны стражей, набирайте очки и избегайте шипов. Оригинальная игра требует подключения к своему серверу; эта мини-игра запускается независимо.

## Публикация

1. Страница игры уже размещена на GitHub Pages по адресу выше.
2. Создайте бота у [@BotFather](https://t.me/BotFather) командой `/newbot`.
3. В Railway создайте сервис из репозитория `RTCcoin1/temple-echo-miniapp`. Файл `railway.json` задаёт команду запуска `python bot.py`.
4. В переменных Railway задайте `BOT_TOKEN` (токен от BotFather) и `WEBAPP_URL` со значением `https://rtccoin1.github.io/temple-echo-miniapp/`.
5. Откройте бота в Telegram и отправьте `/start`.

Для локального запуска скопируйте `.env.example` в `.env`, заполните `BOT_TOKEN` и `WEBAPP_URL`, затем выполните `python -m pip install -r requirements.txt` и `python bot.py`.

Если BotFather запросит домен для Web App, укажите `rtccoin1.github.io`. Не публикуйте `.env` и не отправляйте токен бота в чат. Для Railway задавайте его только как секретную переменную `BOT_TOKEN`.

## Управление

- Телефон: экранные стрелки и кнопка «Огонь».
- Компьютер: WASD или стрелки, пробел — выстрел.
- Кнопка «Назад» Telegram ставит игру на паузу.

Результат и прогресс пока хранятся только во время текущей игровой сессии. Для таблицы рекордов понадобится серверное хранение и проверка `initData` Telegram.
