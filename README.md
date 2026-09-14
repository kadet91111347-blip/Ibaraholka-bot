# АйБарахолка · Telegram-бот + API

Готовый к деплою бэкенд для Telegram Mini App «АйБарахолка»: бот, HTTP API и оплата Telegram Stars.

## Что внутри

- **aiogram 3.x** — Telegram-бот (polling режим)
- **FastAPI** — HTTP API для Mini App
- **SQLite** — хранилище объявлений
- **Telegram Stars (XTR)** — приём платежей за платные размещения (50⭐ / 150⭐)

## Что нужно за 5 минут

### 1. Создать бота в Telegram

1. Открой [@BotFather](https://t.me/BotFather)
2. `/newbot` → придумай имя (например, «АйБарахолка Bot») и username (например, `ibaraholka_bot`)
3. Скопируй токен (вида `1234567890:AAH...xyz`) — это `BOT_TOKEN`
4. `/setmenubutton` → выбери бота → кнопка: «📱 Открыть барахолку» → URL: `https://ibaraholka.p.spru.io/`
5. (опционально) `/setdescription` — задай описание бота
6. (опционально) `/setuserpic` — аватарка

### 2. Деплой на Railway (бесплатно)

#### Шаг 1. Залей код на GitHub
```bash
cd ibaraholka-bot
git init
git add .
git commit -m "init"
# создай репозиторий на github.com и следуй инструкциям
git remote add origin https://github.com/ТВОЙ_ЮЗЕР/ibaraholka-bot.git
git push -u origin main
```

#### Шаг 2. Подключи к Railway
1. Открой [railway.app](https://railway.app) → войди через GitHub
2. **New Project** → **Deploy from GitHub repo** → выбери `ibaraholka-bot`
3. В настройках сервиса → **Variables** → добавь:
   - `BOT_TOKEN` = токен от BotFather
   - `WEBAPP_URL` = `https://ibaraholka.p.spru.io/`
   - `ADMIN_IDS` = твой Telegram ID (можно узнать через @userinfobot)
4. **Settings** → **Deploy** → должен запуститься
5. Смотри логи: должны увидеть `✅ DB initialized`, `🤖 Starting bot polling`, `🌐 Starting API on port 8080`
6. Railway даст тебе URL вида `https://ibaraholka-bot.up.railway.app`

#### Шаг 3. Проверь
- API: `https://ibaraholka-bot.up.railway.app/health` → должен вернуть `{"ok":true}`
- Список объявлений: `https://ibaraholka-bot.up.railway.app/listings`
- Открой своего бота в Telegram → `/start` → нажми «📱 Открыть барахолку» → должно открыться Mini App

### 3. Альтернатива: запуск на своём компьютере (для тестов)

```bash
cd ibaraholka-bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

export BOT_TOKEN="1234567890:AAH..."  # Linux/Mac
set BOT_TOKEN=1234567890:AAH...      # Windows CMD
# $env:BOT_TOKEN="1234567890:AAH..."  # Windows PowerShell

export WEBAPP_URL="https://ibaraholka.p.spru.io/"
python main.py
```

Бот запустится в polling-режиме и начнёт принимать сообщения.

## Подключение WebApp к API

Текущая версия WebApp на https://ibaraholka.p.spru.io/ хранит объявления локально (localStorage). Чтобы заменить на боевой API:

Заменить в `index.html` функции `loadListings`, `addListing`, `renderListings`:

```javascript
const API_URL = "https://ibaraholka-bot.up.railway.app";  // твой Railway URL

async function apiFetch(path, options) {
  const tg = window.Telegram && window.Telegram.WebApp;
  const headers = { "Content-Type": "application/json" };
  if (tg && tg.initData) {
    headers["Authorization"] = "tma " + tg.initData;
  }
  return fetch(API_URL + path, { ...options, headers: { ...headers, ...(options.headers||{}) } });
}

async function loadListings() {
  const r = await apiFetch("/listings");
  return await r.json();
}

async function addListing(l) {
  const r = await apiFetch("/listings", {
    method: "POST",
    body: JSON.stringify(l),
  });
  return await r.json();
}
```

Перед `renderListings()` добавить `await`:

```javascript
async function renderListings() {
  const arr = await loadListings();
  // ...
}
```

После успешной отправки в форме — слать `POST /listings` вместо `addListing()`. При выборе платного тарифа Telegram сам пришлёт инвойс в бот.

## Как это работает (поток)

```
Пользователь → открывает Mini App
              ↓ заполняет форму
              ↓ POST /listings с tier=premium/vip
              ↓
            Бэкенд сохраняет в БД (status=awaiting_payment)
              ↓
            Бот отправляет Stars-инвойс пользователю
              ↓
Пользователь оплачивает звёздами в Telegram
              ↓
Бот получает successful_payment
              ↓
Бэкенд обновляет status='active'
              ↓
Объявление появляется в ленте
```

## Переменные окружения

| Переменная | Обязательна | Описание |
|---|---|---|
| `BOT_TOKEN` | ✅ | Токен бота от @BotFather |
| `WEBAPP_URL` | ⚠️ рекомендуется | URL WebApp (по умолчанию `https://ibaraholka.p.spru.io/`) |
| `ADMIN_IDS` | опционально | Твой Telegram ID для `/stats` и модерации |
| `PORT` | опционально | Порт API (по умолчанию 8080) |

## Файлы

```
ibaraholka-bot/
├── main.py             # Всё в одном: бот + API
├── requirements.txt    # Зависимости Python
├── ibaraholka.db       # SQLite (создаётся автоматически при первом запуске)
└── README.md           # Этот файл
```

## Дальнейшие шаги

- [ ] Модерация объявлений (добавить статус `pending`, ручное подтверждение)
- [ ] Загрузка фото на хостинг (сейчас base64 — для продакшна нужен S3)
- [ ] Поиск по объявлениям
- [ ] Уведомления в бот при отклике
- [ ] Расширенная админка
