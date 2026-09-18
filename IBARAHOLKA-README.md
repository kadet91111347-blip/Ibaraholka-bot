# Айбарахолка · Telegram Mini App

Маркетплейс для Apple-техники внутри Telegram.

## Архитектура

```
┌─────────────────┐         ┌──────────────────┐        ┌────────────────┐
│  Telegram Bot   │ ←─────→ │  FastAPI backend │ ←────→ │  PostgreSQL/   │
│  @Ibaraholka_bot│   HTTPS │   main.py        │        │  SQLite        │
└─────────────────┘         │   (aiogram +     │        │  (Neon/Render) │
         ↓                  │    FastAPI)      │        └────────────────┘
    WebApp API              └──────────────────┘
         ↓                          ↑
┌─────────────────┐         ┌──────────────────┐
│  Mini App       │ ←─────→ │  Render Free     │
│  HTML+JS        │  HTTPS  │  ibaraholka-bot  │
│  Telegram CDN   │         │  .onrender.com   │
└─────────────────┘         └──────────────────┘
```

## Файлы

- **main.py** (6151 lines) — FastAPI API + aiogram bot в одном файле
  - Endpoints: `/listings`, `/payments/{yukassa,ton,activate}`,
    `/deals`, `/user/balance`, `/match/subscribe`, `/admin/*`,
    `/debug/*`, Mini App на `/mini`
  - Бот: хендлеры `/start`, `/help`, `/listings`, deep link `/start=pay_<id>`
  - Канал: `@ibaraholkatyt` — автопубликация VIP/Premium листингов

- **db_adapter.py** (241 line) — унифицированный DB layer
  - SQLite (локально/Render Free) или PostgreSQL (Neon)
  - psycopg2-binary + ThreadedConnectionPool + RealDictCursor
  - 21+ таблиц: listings, conversations, deals, user_balances и др.

- **miniapp/index.html** (1245 lines) — Telegram Mini App frontend
  - Vanilla JS, без фреймворков
  - Telegram WebApp SDK для auth/initData
  - Лента, фильтры, поиск, форма создания листинга,
    оплата (Т-Банк / Звёзды / TON), профиль, избранное

- **render.yaml** — Render deployment config
- **requirements.txt** — aiogram, fastapi, psycopg2-binary, httpx

## Запуск

```bash
# Локально
pip install -r requirements.txt
BOT_TOKEN=... WEBAPP_URL=... uvicorn main:app --port 10000

# Render (auto-deploy из GitHub main)
# Service: ibaraholka-bot (Free plan, Oregon)
```

## Env vars (Render Dashboard)

| Var | Required | Default | Описание |
|-----|----------|---------|----------|
| BOT_TOKEN | ✅ | — | @BotFather token |
| ADMIN_TOKEN | ✅ | — | для /admin/* endpoints |
| CHANNEL_ID | ✅ | @ibaraholkatyt | Telegram channel |
| WEBAPP_URL | ⚠️ | render /mini | URL Mini App |
| DATABASE_URL | ⚠️ | Postgres | если пусто — SQLite |
| ADMIN_IDS | ✅ | — | user_id через запятую |
| YOOMONEY_WALLET | ❌ | 4100119629651495 | для тестовых payments |
| TON_WALLET_ADDRESS | ⚠️ | UQCAhDLD17FVwmprVze2V35mICOqjmEpBdJF-cJyCZqfph-3 | для TON payments |

## Endpoints (84 шт)

- Public: `GET /listings`, `GET /listings/{id}`, `POST /listings`,
  `POST /listings/{id}/view`, `POST /listings/{id}/confirm-paid`
- Auth (tma initData): `GET /profile/me`, `GET /favorites`,
  `POST /favorites`, `GET /deals/*`, `POST /deals/*`
- Payments: `POST /payments/{yukassa,tinkoff,ton}/{create,verify,activate}`
- Mini App: `GET /mini`, `GET /mini/{path}`, `HEAD` (Telegram WebView)
- Admin (x-admin-token): `/admin/listings`, `/admin/wipe-all-listings`,
  `/admin/deals`, `/admin/payouts`, `/admin/stats`, `/admin/demo-mode`
- Debug (no-auth, sensitive): `/debug/state`, `/debug/version`,
  `/debug/toggle-demo`, `/debug/post-channel-test`

## Channels

- Bot: `@Ibaraholka_bot` — принимает команды, deep links, кнопочное меню
- Channel: `@ibaraholkatyt` — автопубликация VIP/Premium листингов с короной 👑

## Tier'ы

- 🆓 **free** — простая публикация в ленте, 0₽
- ⭐ **premium** — TOP-размещение, 70₽
- 👑 **vip** — корона + приоритет, 210₽

DEMO_MODE=1 (Render Free) пропускает payment для тестов.

## Заметки для разработчиков
- **Кросс-DB**: код работает на SQLite (Render Free fallback) и Postgres (DATABASE_URL). Использовать `_next_id()` для генерации BIGINT id; не использовать `BIGSERIAL` / `GENERATED ALWAYS AS IDENTITY`; не использовать Postgres-only синтаксис (`extract(epoch from now())::bigint` → `int(time.time())`, `::float` → `CAST(... AS FLOAT)`).
- **Колоночные миграции**: helper `_add_column_if_not_exists(table, column, type)` на `init_db` для новых полей (PRAGMA table_info для SQLite, information_schema для Postgres).
- **demo mode**: `DEMO_MODE=1` (env) или POST `/debug/toggle-demo?enabled=true|false|null` (runtime) — пропускает payment gate для тестов.
- **Manual Deploy**: если Render Free план залип на одном sha, Manual Deploy через Dashboard → Service → Manual Deploy → Deploy latest commit. Hooks могут не срабатывать если у Service есть failed deploy'и.
