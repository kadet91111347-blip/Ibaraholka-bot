# 🚀 Production Checklist — Ibaraholka Bot

## ✅ Что работает прямо сейчас (v86, sha=b17c78f)

| Компонент | Статус | Комментарий |
|---|---|---|
| FastAPI app (80+ endpoints) | ✅ | Uvicorn 0.32, lifespan-managed startup |
| aiogram 3.15 bot | ✅ | Polling + WebApp auth + Stars payments |
| PostgreSQL (Neon) | ✅ | USE_POSTGRES=true, кросс-DB SQL, миграции |
| Mini App (104 KB) | ✅ | Vanilla JS, Telegram SDK 6.0+, hidden fallback for Chrome |
| Stars (XTR) payment | ✅ | createInvoiceLink → tg.openInvoice → auto-activate |
| TON payment | ✅ | Wallet UQCA..., manual confirm, 0.75 TON/VIP |
| ЮKassa/Тинькофф | ✅ | Test mode, manual confirm + activate |
| Rate limiting | ✅ | 10 POST /listings/min, 20 invoices/min → 429 |
| CORS / auth | ✅ | tma + initData signature verification |
| Admin panel | ✅ | ⚙️ /admin/ui/me + /admin/ui/stats |
| Reports / moderation | ✅ | 6 reasons, admin resolve |
| Sentry / health checks | ✅ | /health checks {db,bot,dispatcher} |
| OpenAPI tags | ⚠️ | 0 tags в документе (не критично) |
| Wipe secret | ✅ | sha256("wipe:"+BOT_TOKEN)[:24] |

## 📊 Тесты (pytest 9.1.1, last run: 50/50 passed in 43s)

```
tests/test_health.py    —  8 tests  /health /debug/version /mini /openapi
tests/test_listings.py  — 12 tests  create FREE/VIP/Premium, validate, status
tests/test_payments.py  —  5 tests  yukassa/ton/stars + payment_urls
tests/test_features.py  — 13 tests  favorites/search/profile/deals/match
tests/test_reports.py   — 13 tests  report reasons / admin resolve / self-protection
```

## 🔐 Что нужно для запуска (production)

### 1. Environment variables (Render Dashboard → Environment)

| Переменная | Сейчас | Для прод-запуска |
|---|---|---|
| `BOT_TOKEN` | 8925325612:AAFB... | ✅ уже есть |
| `CHANNEL_ID` | @ibaraholkatyt | ✅ уже есть |
| `WEBAPP_URL` | https://ibaraholka-bot.onrender.com/mini | ✅ уже есть |
| `DATABASE_URL` | postgresql://neondb_owner:npg_... | ✅ Neon (free tier 0.5 ГБ) |
| `DEMO_MODE` | "0" | ✅ не demo |
| `DB_PATH` | (unset) | ⚠️ не нужно пока Neon жив |
| `ADMIN_TOKEN` | (set, value защищён) | ✅ используется для /admin/* |
| `ADMIN_IDS` | (set on Render) | ✅ для /admin/ui/* (uid 748834052 = Саша) |
| `TINKOFF_TERMINAL_KEY` | (set) | ⚠️ test mode → нужно real |
| `TINKOFF_PASSWORD` | (set) | ⚠️ test mode → нужно real |
| `YOOKASSA_SHOP_ID` | (set) | ⚠️ test mode |
| `YOOKASSA_SECRET_KEY` | (set) | ⚠️ test mode |
| `TON_WALLET_ADDRESS` | UQCAhDLD17FVwmpr... | ⚠️ Сашин кошелёк? Мой — нужно перевести на свой |
| `TON_API_KEY` | (unset) | ⚠️ для авто-проверки платежей через tonapi |
| `WIPE_SECRET` | implicit sha256(BOT_TOKEN) | ✅ auto |
| `SENTRY_DSN` | (unset) | опционально |

### 2. Что нужно сделать руками перед запуском

1. **🔑 TON кошелёк** — сейчас в коде `UQCAhDLD17FVwmprVze2V35mICOqjmEpBdJF-cJyCZqfph-3`. Нужно подтвердить — это Сашин? Иначе деньги пойдут не тому.
2. **💳 Платёжные провайдеры** — для реальных платежей через Tinkoff/ЮKassa нужно:
   - Зарегистрироваться в Tinkoff Business (₽/мес)
   - Зарегистрироваться в ЮKassa (бесплатно, ~3.5% комиссия)
   - Заменить test tokens на production
3. **⭐ Звёзды Telegram** — работают автоматически без провайдера, комиссия 30% берёт Telegram. Доход с VIP: 150 ⭐ ≈ 525 ₽ → ~370 ₽ после комиссии.
4. **📋 Юридика** — для приёма платежей нужна самозанятость (НПД, 6%) или ИП. Без этого нельзя легально продавать.
5. **📜 Оферта** — ссылка на пользовательское соглашение в боте
6. **🛡 Модерация** — сейчас админ один (Саша). Добавить второго админа, доверить модерацию

### 3. Что не критично, но желательно

| Что | Приоритет | Трудозатраты |
|---|---|---|
| OpenAPI tags (все endpoints) | 🟡 medium | 2 часа |
| Telegram login button (вместо ручного /start) | 🟢 low | 1 час |
| Sentry alerts на 5xx | 🟡 medium | 30 мин |
| CI/CD (GitHub Actions pytest на каждый push) | 🟡 medium | 1 час |
| Dockerfile + docker-compose для локального запуска | ✅ done | — |
| README + .env.example | ✅ done | — |
| Render Starter ($7/мес) — нет cold start 30с | 🟡 medium | $7/мес |
| Render Persistent Disk ($1/мес/1ГБ) | 🟢 low если Neon | $1/мес |
| Переезд на свой домен (ibaraholka.ru) | 🟢 low | 1 час |

### 4. Что НЕ работает (или не проверено)

| Что | Почему |
|---|---|
| `/health → ok:true` для Sentry | без SENTRY_DSN |
| Поиск с фильтрами по городу/цене до 100р | работает, но без пагинации |
| Уведомления о платежах в админ-чат | работает, но не настроен чат для ADMIN_IDS |
| Email-уведомления | нет, нужно интегрировать SMTP |

## 📈 Метрики для запуска

| Метрика | Текущая | Цель для прода |
|---|---|---|
| Latency p50 (/health) | 0.64s | <0.5s |
| Latency p95 (/listings) | <1s | <2s |
| Apdex (нет логов) | ? | >0.7 |
| Listings count | 0 (чистый старт) | 50+ в первую неделю |
| Users (first week) | 1 (Саша) | 10 |
| Conversion (listing → pay) | 0% | 5-15% |

## 🛠 Команды для развертывания

### Локальный запуск (для тестов)
```bash
git clone https://github.com/kadet91111347-blip/Ibaraholka-bot.git
cd Ibaraholka-bot
cp .env.example .env       # заполнить токены
docker-compose up -d       # запуск + локальный postgres
curl localhost:10000/health
```

### Render deploy
1. Подключить репо к Render (auto-deploy ON)
2. Установить env vars из .env.example
3. Деплой автоматический при push в main

### Wipe listings (debug)
```bash
WIPE_SECRET=$(echo -n "wipe:$BOT_TOKEN" | sha256sum | cut -c1-24)
curl -X POST https://ibaraholka-bot.onrender.com/admin/wipe-all-listings \
  -H "x-wipe-secret: $WIPE_SECRET"
```
