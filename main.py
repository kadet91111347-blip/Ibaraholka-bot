from __future__ import annotations  # PEP 563: defer all annotations so PEP 585/604 syntax works on Python 3.7+
"""
АйБарахолка · Telegram-бот + FastAPI бэкенд

Запуск:
    export BOT_TOKEN="..."      # от @BotFather
    export WEBAPP_URL="..."     # URL опубликованного WebApp
    export PORT=8080             # опционально
    python main.py
"""
import os
import asyncio
import logging
# import sqlite3 (now via db_adapter)
import json
import time
import hmac
import hashlib
import urllib.parse
import secrets as _secrets
import uuid as _uuid
_uuid4 = _uuid.uuid4
import httpx
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request, Query, Depends
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from db_adapter import db_cursor, get_db_connection, migrate_sqlite_to_pg, USE_POSTGRES
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# ============================================================
# Config
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
import re as _re_webapp
_raw_webapp = os.getenv("WEBAPP_URL", "https://ibaraholka.p.spru.io/v6.html").strip()
# Hardcoded correct value as ultimate fallback
_correct_webapp = "https://ibaraholka.p.spru.io/v6.html"
# Try env var first, apply fixes
WEBAPP_URL = _raw_webapp
if WEBAPP_URL and '://' not in WEBAPP_URL:
    WEBAPP_URL = 'https://' + WEBAPP_URL
# Fix missing slash between domain and page (e.g. .iov6.html -> .io/v6.html)
WEBAPP_URL = _re_webapp.sub(r'(spru\.io)([^/:])', r'\1/\2', WEBAPP_URL)
# If still broken (no v6.html suffix and looks like a spru domain), force correct
if 'spru.io' in WEBAPP_URL and not WEBAPP_URL.endswith('/v6.html') and not WEBAPP_URL.endswith('/'):
    WEBAPP_URL = WEBAPP_URL.rstrip('/') + '/v6.html'
# Sanity check: if WEBAPP_URL doesn't contain /v6.html at all, use hardcoded
if 'spru.io' in WEBAPP_URL and '/v6.html' not in WEBAPP_URL:
    WEBAPP_URL = _correct_webapp
print(f'[startup] WEBAPP_URL={WEBAPP_URL}')
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ibaraholkatyt").strip()
# Admin token for privileged API operations (delete, publish, etc.)
# In production set via ADMIN_TOKEN env var; fallback only for emergency local dev
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
# DEMO_MODE: 1 = accept requests without Telegram initData (for testing)
#            0 = require real Telegram WebApp authorization (production)
DEMO_MODE = os.getenv("DEMO_MODE", "0").strip() == "1"

# Don't crash if BOT_TOKEN missing — start API anyway, log warning
if not BOT_TOKEN:
    print("⚠️  WARNING: BOT_TOKEN not set. Bot won't start, but API will run.")
    print("   Set BOT_TOKEN in Railway → Variables to enable the bot.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ibaraholka")

# Initialize bot only if token is present
bot = None
dp = None
if BOT_TOKEN:
    try:
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher()
        logger.info("Bot initialized")
    except Exception as e:
        logger.error(f"Failed to init bot: {e}")
        bot = None
        dp = None

DB_FILE = os.getenv("DB_PATH", "ibaraholka.db").strip()
# If /data directory exists (Railway volume), use it
if not os.getenv("DB_PATH") and os.path.isdir("/data") and os.access("/data", os.W_OK):
    DB_FILE = "/data/ibaraholka.db"
# Ensure parent directory exists (for /data/ibaraholka.db)
db_dir = os.path.dirname(os.path.abspath(DB_FILE))
if db_dir:
    os.makedirs(db_dir, exist_ok=True)
print(f"[DB] Using DB file: {DB_FILE}", flush=True)


# ============================================================
# Database
# ============================================================

def init_db():
    with db_cursor() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            user_username TEXT,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            price INTEGER DEFAULT 0,
            cat TEXT NOT NULL,
            type TEXT DEFAULT 'sell',
            contact TEXT NOT NULL,
            photo TEXT DEFAULT '',
            tier TEXT DEFAULT 'free',
            city TEXT DEFAULT 'Москва',
            status TEXT DEFAULT 'pending',
            created INTEGER NOT NULL,
            expires_at INTEGER,
            channel_message_id INTEGER DEFAULT NULL,
            paid_at INTEGER DEFAULT NULL
        );
        """)
        # Add column if upgrading (SQLite supports ALTER TABLE ADD COLUMN with try/except)
        try:
            conn.execute("ALTER TABLE listings ADD COLUMN channel_message_id INTEGER DEFAULT NULL")
        except Exception:
            pass
        # Postgres upgrade: convert INTEGER columns to BIGINT to fit Telegram user_ids (8-9 digits)
        try:
            conn.execute("ALTER TABLE listings ALTER COLUMN user_id TYPE BIGINT")
            conn.execute("ALTER TABLE listings ALTER COLUMN price TYPE BIGINT")
            conn.execute("ALTER TABLE listings ALTER COLUMN created TYPE BIGINT")
            conn.execute("ALTER TABLE listings ALTER COLUMN expires_at TYPE BIGINT")
            conn.execute("ALTER TABLE listings ALTER COLUMN channel_message_id TYPE BIGINT")
            conn.execute("ALTER TABLE listings ADD COLUMN IF NOT EXISTS paid_at INTEGER DEFAULT NULL")
        except Exception:
            pass  # SQLite doesn't support ALTER COLUMN — silent skip
        # Same upgrade for conversations / ai_responses / variant_stats / user_profiles / learned_patterns
        for tbl in ('conversations', 'ai_responses', 'variant_stats', 'user_profiles', 'learned_patterns'):
            try:
                conn.execute(f"ALTER TABLE {tbl} ALTER COLUMN user_id TYPE BIGINT")
                conn.execute(f"ALTER TABLE {tbl} ALTER COLUMN created TYPE BIGINT")
            except Exception:
                pass
        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_status ON listings(status);
        CREATE INDEX IF NOT EXISTS idx_tier ON listings(tier);
        CREATE INDEX IF NOT EXISTS idx_cat ON listings(cat);
        CREATE INDEX IF NOT EXISTS idx_user ON listings(user_id);
        CREATE INDEX IF NOT EXISTS idx_created ON listings(created);
        """)

        # ===== SELF-LEARNING BOT TABLES =====
        conn.executescript("""
        -- История диалогов
        CREATE TABLE IF NOT EXISTS conversations (
            id BIGINT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            user_message TEXT NOT NULL,
            bot_response TEXT NOT NULL,
            intent TEXT NOT NULL,
            response_variant INTEGER DEFAULT 0,
            created INTEGER NOT NULL,
            feedback TEXT DEFAULT NULL,
            rating INTEGER DEFAULT NULL,
            led_to_sale INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
        CREATE INDEX IF NOT EXISTS idx_conv_intent ON conversations(intent);
        CREATE INDEX IF NOT EXISTS idx_conv_created ON conversations(created);

        -- Сгенерированные AI ответы (после обучения)
        CREATE TABLE IF NOT EXISTS ai_responses (
            id BIGINT PRIMARY KEY,
            intent TEXT NOT NULL,
            user_pattern TEXT NOT NULL,
            ai_response TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            uses INTEGER DEFAULT 0,
            success_rate REAL DEFAULT 0.0,
            created INTEGER NOT NULL,
            updated INTEGER NOT NULL,
            UNIQUE(intent, user_pattern)
        );
        CREATE INDEX IF NOT EXISTS idx_ai_intent ON ai_responses(intent);

        -- Обучение: какие варианты работают лучше
        CREATE TABLE IF NOT EXISTS variant_stats (
            id BIGINT PRIMARY KEY,
            intent TEXT NOT NULL,
            variant_idx INTEGER NOT NULL,
            uses INTEGER DEFAULT 0,
            positive INTEGER DEFAULT 0,
            negative INTEGER DEFAULT 0,
            last_updated INTEGER NOT NULL,
            UNIQUE(intent, variant_idx)
        );

        -- Паттерны обучения (что бот уже понял)
        CREATE TABLE IF NOT EXISTS learned_patterns (
            id BIGINT PRIMARY KEY,
            pattern TEXT NOT NULL UNIQUE,
            intent TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            created INTEGER NOT NULL
        );

        -- Профиль клиента (что он предпочитает)
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            preferred_intent TEXT DEFAULT '',
            last_messages TEXT DEFAULT '',
            messages_count INTEGER DEFAULT 0,
            last_active INTEGER NOT NULL,
            is_lead INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        );

        -- ===== ADS / IB COINS =====
        -- Рекламные креативы (что показывать юзеру за IB Coins)
        CREATE TABLE IF NOT EXISTS ad_creatives (
            id BIGINT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            click_url TEXT DEFAULT '',
            reward_coins INTEGER NOT NULL DEFAULT 10,
            duration_sec INTEGER NOT NULL DEFAULT 10,
            enabled INTEGER NOT NULL DEFAULT 1,
            weight INTEGER NOT NULL DEFAULT 1,
            created INTEGER NOT NULL,
            shown_count INTEGER DEFAULT 0,
            click_count INTEGER DEFAULT 0
        );

        -- Просмотры рекламы (антифрод: 1 просмотр = +N монет, не чаще 1 раза в 30с на юзера)
        CREATE TABLE IF NOT EXISTS ad_views (
            id BIGINT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            ad_id BIGINT NOT NULL,
            coins_credited INTEGER NOT NULL,
            ip TEXT DEFAULT '',
            created INTEGER NOT NULL,
            completed INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_views_user ON ad_views(user_id);
        CREATE INDEX IF NOT EXISTS idx_views_ad ON ad_views(ad_id);
        CREATE INDEX IF NOT EXISTS idx_views_created ON ad_views(created);

        -- Баланс внутренней валюты (IB Coins): 1 IB Coin = 1 Telegram Star
        CREATE TABLE IF NOT EXISTS user_balances (
            user_id BIGINT PRIMARY KEY,
            coins INTEGER NOT NULL DEFAULT 0,
            total_earned INTEGER NOT NULL DEFAULT 0,
            total_spent INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_balance_coins ON user_balances(coins);

        -- ===== ESCROW / DEALS / ВЫВОД СРЕДСТВ =====
        -- Сделка между покупателем и продавцом, деньги в гаранте до подтверждения получения.
        CREATE TABLE IF NOT EXISTS deals (
            id TEXT PRIMARY KEY,
            listing_id TEXT NOT NULL,
            buyer_id BIGINT NOT NULL,
            buyer_name TEXT,
            buyer_username TEXT,
            seller_id BIGINT NOT NULL,
            seller_name TEXT,
            seller_username TEXT,
            amount_rub BIGINT NOT NULL,           -- цена сделки в рублях
            amount_nano BIGINT,                    -- цена в TON (если оплата TON), иначе NULL
            currency TEXT NOT NULL,                -- 'RUB' | 'TON'
            payment_method TEXT NOT NULL,          -- 'tinkoff' | 'yukassa' | 'ton'
            status TEXT NOT NULL DEFAULT 'awaiting_payment',
                -- awaiting_payment → escrowed → shipped → released
                --                  ↘ disputed → refunded / released (admin)
                --                  ↘ cancelled (до оплаты)
            shipping_address TEXT,
            shipping_city TEXT,
            tracking TEXT,
            dispute_reason TEXT,
            dispute_resolution TEXT,
            escrow_tx_hash TEXT,                   -- tx хеш входящего TON платежа или label Тинькофф
            payout_tx_hash TEXT,                   -- tx хеш исходящего TON продавцу при release
            created INTEGER NOT NULL,
            paid_at INTEGER,
            shipped_at INTEGER,
            confirmed_at INTEGER,
            closed_at INTEGER,
            auto_release_at INTEGER                 -- когда автоподтверждение (shipped + 5 дней)
        );
        CREATE INDEX IF NOT EXISTS idx_deals_buyer ON deals(buyer_id);
        CREATE INDEX IF NOT EXISTS idx_deals_seller ON deals(seller_id);
        CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status);
        CREATE INDEX IF NOT EXISTS idx_deals_listing ON deals(listing_id);

        -- Сообщения внутри сделки (чат покупатель ↔ продавец)
        CREATE TABLE IF NOT EXISTS deal_messages (
            id BIGSERIAL PRIMARY KEY,
            deal_id TEXT NOT NULL,
            from_user_id BIGINT NOT NULL,
            text TEXT,
            photo_url TEXT,
            created INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_deal_messages_deal ON deal_messages(deal_id);

        -- Балансы продавцов для вывода (₽ и TON отдельно)
        CREATE TABLE IF NOT EXISTS seller_balances (
            user_id BIGINT NOT NULL,
            currency TEXT NOT NULL,                -- 'RUB' | 'TON'
            amount BIGINT NOT NULL DEFAULT 0,      -- в копейках (RUB) или нанотонах (TON)
            updated INTEGER NOT NULL,
            PRIMARY KEY (user_id, currency)
        );

        -- Заявки на вывод средств продавцом
        CREATE TABLE IF NOT EXISTS payouts (
            id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            currency TEXT NOT NULL,                -- 'RUB' | 'TON'
            amount BIGINT NOT NULL,
            destination TEXT NOT NULL,             -- карта/телефон для RUB или TON-адрес для TON
            status TEXT NOT NULL DEFAULT 'pending',-- pending → completed | failed
            tx_hash TEXT,
            created INTEGER NOT NULL,
            completed INTEGER,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_payouts_user ON payouts(user_id);
        CREATE INDEX IF NOT EXISTS idx_payouts_status ON payouts(status);

        -- ===== БОТ-ПОДБИРАТЕЛЬ / Match agent =====
        -- Юзер подписывается на запрос типа "iPhone 13 до 30К в Москве" — бот пушит подходящие объявления
        CREATE TABLE IF NOT EXISTS match_subscriptions (
            id TEXT PRIMARY KEY,                  -- MS-XXXXXX
            user_id BIGINT NOT NULL,
            user_name TEXT,
            user_username TEXT,
            query TEXT NOT NULL,                  -- сырой запрос юзера: "iPhone 13 до 30К в Москве, чёрный"
            keywords TEXT NOT NULL,               -- нормализованные ключевые слова для матчинга (через запятую)
            cat TEXT,                             -- iphone / airpods / ipad / mac / watch / accs / NULL
            max_price_rub BIGINT,                 -- NULL если не указано
            city TEXT,                            -- NULL если любой
            color TEXT,                           -- чёрный / белый / NULL
            extra TEXT,                           -- любые доп. пожелания: "в идеале", "без царапин"
            active INTEGER NOT NULL DEFAULT 1,    -- 1 = активна, 0 = отключена
            is_free INTEGER NOT NULL DEFAULT 0,   -- 1 = бесплатная первая подписка
            paid_until INTEGER,                   -- unix sec — когда заканчивается оплаченный период
            created INTEGER NOT NULL,
            last_notified INTEGER                 -- для rate-limit: не спамить чаще 1 раза в минуту
        );
        CREATE INDEX IF NOT EXISTS idx_match_user ON match_subscriptions(user_id);
        CREATE INDEX IF NOT EXISTS idx_match_active ON match_subscriptions(active) WHERE active=1;
        CREATE INDEX IF NOT EXISTS idx_match_cat ON match_subscriptions(cat);
        CREATE INDEX IF NOT EXISTS idx_match_city ON match_subscriptions(city);

        -- Лог отправленных матчей (чтобы не дублировать пуши)
        CREATE TABLE IF NOT EXISTS match_log (
            id BIGSERIAL PRIMARY KEY,
            subscription_id TEXT NOT NULL,
            listing_id TEXT NOT NULL,
            sent_at INTEGER NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 1   -- 0 если бот не смог доставить (юзер заблокировал)
        );
        CREATE INDEX IF NOT EXISTS idx_match_log_sub ON match_log(subscription_id);
        CREATE INDEX IF NOT EXISTS idx_match_log_listing ON match_log(listing_id);
        """)

        # ===== REFERRALS — Реф-лесенка =====
        # Хранит кто кого привёл и какие бонусы начислены
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id BIGSERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                referred_id BIGINT NOT NULL UNIQUE,
                referred_username TEXT,
                referred_first_name TEXT,
                created INTEGER NOT NULL,
                bonus_granted INTEGER DEFAULT 0,
                bonus_type TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referrals_created ON referrals(created)
        """)
        # Бонусы за реф-лесенку: milestone → кол-во приглашённых → тип бонуса
        # 1 = 5 coins, 3 = 10 coins, 5 = 1 день VIP, 15 = 3 дня VIP, 25 = 7 дней VIP
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_bonuses (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                milestone INTEGER NOT NULL,
                bonus_type TEXT NOT NULL,
                bonus_value TEXT NOT NULL,
                created INTEGER NOT NULL,
                UNIQUE(user_id, milestone)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_referral_bonuses_user ON referral_bonuses(user_id)
        """)

        # ===== ИЗБРАННОЕ / FAVORITES =====
        conn.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                listing_id TEXT NOT NULL,
                created INTEGER NOT NULL,
                UNIQUE(user_id, listing_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_favorites_created ON favorites(created DESC)")

        # ===== ОТЗЫВЫ НА ПРОДАВЦОВ / REVIEWS =====
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id BIGSERIAL PRIMARY KEY,
                deal_id TEXT,
                seller_id BIGINT NOT NULL,
                buyer_id BIGINT NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                text TEXT,
                created INTEGER NOT NULL,
                UNIQUE(deal_id, buyer_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_seller ON reviews(seller_id, created DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reviews_buyer ON reviews(buyer_id)")

        # ===== ПРОСМОТРЫ ОБЪЯВЛЕНИЙ / LISTING VIEWS =====
        # Для счётчика "X человек смотрят" + аналитики продавцу
        conn.execute("""
            CREATE TABLE IF NOT EXISTS listing_views (
                id BIGSERIAL PRIMARY KEY,
                listing_id TEXT NOT NULL,
                viewer_id BIGINT,
                created INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listing_views_listing ON listing_views(listing_id, created DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_listing_views_recent ON listing_views(created DESC)")

        # ===== СОХРАНЁННЫЕ ФИЛЬТРЫ / SAVED FILTERS =====
        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_filters (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                cat TEXT,
                city TEXT,
                max_price INTEGER,
                query TEXT,
                created INTEGER NOT NULL,
                UNIQUE(user_id, name)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_filters_user ON saved_filters(user_id)")


# ============================================================
# Telegram initData validation
# ============================================================
def validate_init_data(init_data: str) -> Dict[str, Any]:
    """Validate Telegram Mini App initData signature (HMAC-SHA256)."""
    if not init_data:
        raise HTTPException(401, "No initData")
    try:
        params = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        hash_val = params.pop("hash", "")
        if not hash_val:
            raise HTTPException(401, "No hash in initData")
        # Build check string: key=value lines sorted by key
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        # Secret = HMAC-SHA256(key="WebAppData", msg=BOT_TOKEN)
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, hash_val):
            raise HTTPException(401, "Invalid initData signature")
        # Parse user JSON
        user_json = params.get("user", "{}")
        return json.loads(user_json)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, f"Invalid initData: {e}")


async def get_user(authorization: str = Header(None)) -> Dict[str, Any]:
    """Get Telegram user from Authorization: tma <initData>.

    If DEMO_MODE=1 (module-level), accept demo requests without initData (for testing).
    In production DEMO_MODE=0 — requires real Telegram WebApp initData.

    When initData is provided but signature validation fails, we still try to extract
    the user from initData params so demos still work but as real-looking users.
    """
    if not authorization or not authorization.startswith("tma "):
        if DEMO_MODE:
            # Accept demo request (bypasses Telegram auth for browser testing)
            return {
                "id": 999999,
                "first_name": "Demo",
                "username": "Izdelie0810",
                "_demo": True,
            }
        raise HTTPException(401, "Authorization header required: 'tma <initData>'")

    raw = authorization[4:]
    try:
        return validate_init_data(raw)
    except HTTPException as e:
        # Signature validation failed: still try to extract user from initData
        # params so we can identify the user even on unsupported Telegram domains
        # (where initData is empty or has hash=unsupported_domain).
        try:
            params = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
            user_json = params.get("user")
            if user_json:
                user_obj = json.loads(user_json)
                user_id = int(user_obj.get("id", 0))
                if user_id > 0:
                    return {
                        "id": user_id,
                        "first_name": user_obj.get("first_name", "User"),
                        "username": user_obj.get("username"),
                        "_unverified": True,  # Signature not checked (Telegram domain not approved)
                    }
        except Exception:
            pass
        raise

# ============================================================
# Models
# ============================================================
TIER_PRICES = {"premium": 50, "vip": 150}  # Stars
TIER_DURATIONS = {"premium": 24 * 3600, "vip": 7 * 24 * 3600}  # seconds
TIER_LABELS = {"free": "Бесплатно", "premium": "⭐ TOP 24ч (50⭐)", "vip": "👑 VIP 7 дней (150⭐)"}

# TON Connect prices (in TON; ~280 RUB/TON)
TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "UQCAhDLD17FVwmprVze2V35mICOqjmEpBdJF-cJyCZqfph-3").strip()
TON_PRICES = {"premium": 0.25, "vip": 0.75}  # TON
TONCENTER_API = os.getenv("TONCENTER_API", "https://toncenter.com/api/v2")
TON_NANOTON = 1_000_000_000


class ListingIn(BaseModel):
    title: str = Field(..., min_length=3, max_length=120)
    description: str = Field(default="", max_length=2000)
    price: int = Field(default=0, ge=0, le=10_000_000)
    cat: str = Field(..., pattern="^(iphone|airpods|ipad|mac|watch|accs)$")
    type: str = Field(default="sell", pattern="^(sell|buy|exchange|opt)$")
    contact: str = Field(..., min_length=3, max_length=120)
    photo: str = Field(default="", max_length=5_000_000)  # base64 dataURL
    tier: str = Field(default="free", pattern="^(free|premium|vip)$")
    city: str = Field(default="Москва", max_length=60)


# ============================================================
# Bot handlers
# ============================================================
async def send_invoice_for_listing(message: types.Message, listing_id: str, tier: str):
    """Re-send Stars invoice for given listing (used by /start=pay_<id>_<tier> deep-link)."""
    user = {"id": message.from_user.id, "username": message.from_user.username or "", "first_name": message.from_user.first_name or ""}
    with db_cursor() as conn:
        row = conn.execute("SELECT title, price, city FROM listings WHERE id=? AND user_id=?", (listing_id, user["id"])).fetchone()
    if not row:
        await message.answer("❌ Объявление не найдено. Открой Mini App заново.")
        return
    title, price, city = row
    amount = TIER_PRICES.get(tier)
    if not amount:
        await message.answer(f"❌ Неизвестный тариф: {tier}")
        return
    tier_name = "TOP 24 часа" if tier == "premium" else "VIP 7 дней"
    try:
        invoice_payload = {
            "chat_id": str(user["id"]),
            "title": f"{tier_name} · {title[:40]}",
            "description": f"📱 <b>{title}</b>\n\n💰 {price:,} ₽ · 📍 {city}\n\n<b>{tier_name}</b>".replace(",", " "),
            "payload": json.dumps({"listing_id": listing_id, "tier": tier}),
            "provider_token": "",
            "currency": "XTR",
            "prices": json.dumps([{"label": tier_name, "amount": amount}]),
        }
        import urllib.request, urllib.parse as _up
        data = _up.urlencode(invoice_payload).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendInvoice", data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        if result.get("ok"):
            await message.answer(f"✅ Инвойс на оплату <b>{tier_name}</b> отправлен выше — нажми <b>«Оплатить ⭐»</b>.")
        else:
            await message.answer(f"❌ Ошибка отправки инвойса: {result}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    args = message.text.split(maxsplit=1)
    payload = args[1] if len(args) > 1 else ""

    text = (
        "👋 <b>Привет! Я — АйБарахолка</b>\n\n"
        "Здесь можно:\n"
        "📱 продать iPhone / AirPods / технику Apple\n"
        "💰 купить по цене ниже магазина\n"
        "🔄 обменять свой аппарат на другой\n"
        "🏪 оптовикам — продавать партии\n\n"
        "Нажми кнопку, чтобы открыть барахолку:"
    )
    if payload.startswith("listing_"):
        # Deep link to specific listing
        text += "\n\n<i>Открываю объявление...</i>"

    # Pay deep-link: pay_<listing_id>_<tier> → re-send invoice
    if payload.startswith("pay_"):
        try:
            parts = payload.split("_", 2)  # ["pay", "<id>", "<tier>"]
            if len(parts) == 3:
                listing_id = parts[1]
                tier = parts[2]
                # Re-send invoice via TIER_PRICES path
                await send_invoice_for_listing(message, listing_id, tier)
                return
        except Exception as e:
            logger.error(f"pay_ handler failed: {e}")

    # Referral landing — payload like 'ref_12345' or 'ref_748834052'
    if payload.startswith("ref_"):
        try:
            referrer_id = int(payload[4:])
            referred_id = int(message.from_user.id)
            if referrer_id >= 1000 and referred_id >= 1000 and referrer_id != referred_id:
                # Register referral (UNIQUE on referred_id — ignore on conflict)
                try:
                    now = int(time.time())
                    with db_cursor() as conn:
                        cur = conn.execute(
                            "INSERT INTO referrals (referrer_id, referred_id, referred_username, referred_first_name, created) "
                            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(referred_id) DO NOTHING RETURNING id",
                            (
                                referrer_id,
                                referred_id,
                                message.from_user.username or "",
                                message.from_user.first_name or "",
                                now,
                            ),
                        )
                        # Try fetch row (Postgres) — sqlite may not support RETURNING
                        inserted = False
                        try:
                            row = cur.fetchone()
                            inserted = bool(row)
                        except Exception:
                            inserted = True  # assume inserted for sqlite path
                        # Check milestones for referrer
                        if inserted:
                            granted = _check_and_grant_milestones(conn, referrer_id)
                            conn.commit()
                            # Notify referrer
                            if bot is not None:
                                try:
                                    await bot.send_message(
                                        referrer_id,
                                        f"🎉 <b>Новый реферал!</b>\n\n"
                                        f"Кто-то пришёл по твоей ссылке. Открой /refs чтобы посмотреть прогресс.",
                                    )
                                except Exception:
                                    pass
                            bonus_text = (
                                "\n\n🎁 <b>Бонус:</b> ты только что принёс +1 реферала тому, кто тебя позвал. "
                                "Если у тебя ещё нет подписки — загляни в Mini App, там 🔔 Бот-подбиратель и 🎁 Реф-лесенка."
                            )
                            text += bonus_text
                except Exception as e:
                    logging.warning(f"start ref track error: {e}")
        except ValueError:
            pass

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Открыть барахолку", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]),
    )


@dp.message(Command("id"))
async def cmd_myid(message: types.Message):
    """Reply with the user's own Telegram ID (useful for setting up ADMIN_IDS)."""
    u = message.from_user
    await message.answer(
        f"🆔 <b>Твой Telegram ID:</b> <code>{u.id}</code>\n\n"
        f"Username: @{u.username or '—'}\n"
        f"Имя: {u.first_name or ''} {u.last_name or ''}\n\n"
        f"Этот ID нужно использовать для <code>ADMIN_IDS</code> в Railway Variables."
    )


# ============================================================
# AI-АССИСТЕНТ: автоответы 24/7 + скрипты продаж
# ============================================================

import random as _random

SALES_SCRIPTS = {
    "greeting": [
        "👋 Привет! Я Саша-бот, помощник @Izdelie0810.\n\nПомогу выбрать iPhone, AirPods или технику Apple. Что ищешь?",
        "👋 Здравствуйте! Рад, что написали. У нас всегда свежие объявления iPhone, AirPods, Mac. Подсказать что-то конкретное?",
        "Привет! Я бот-ассистент АйБарахолки. Подскажу по ценам, наличию, помогу оформить. Чем помочь?",
    ],
    "pricing_question": [
        "📊 У нас цены ниже магазина на 15-30%, потому что без посредников.\n\nНапример:\n— iPhone 13 от 39 900₽\n— iPhone 14 Pro от 75 000₽\n— AirPods Pro 2 от 22 000₽\n\nЧто интересует?",
        "💰 Все цены в канале @ibaraholkatyt. Там же фото, состояние, контакты продавцов.\n\nИли напишите модель — скажу сколько у нас стоит.",
    ],
    "iphone_buy": [
        "📱 Отлично! Какой iPhone ищете? Модель, объём памяти, бюджет?\n\nУ нас обычно в наличии: iPhone 13/14/15. Все проверены, полный комплект.",
        "📱 Могу подсказать. Расскажите:\n— Какая модель (13/14/15/Pro)?\n— Сколько памяти (128/256/512 ГБ)?\n— Какой бюджет?\n\nПодберу лучший вариант из канала.",
    ],
    "iphone_sell": [
        "💼 Хотите продать? Сделаем за 5 минут:\n\n1. Откройте @Ibaraholka_bot → «+ Подать»\n2. Заполните форму (фото, модель, цена)\n3. Выберите тариф — Free / TOP / VIP\n4. Оплатите ⭐ Stars (внутри Telegram, без карт)\n\nОбъявление сразу в канале @ibaraholkatyt. Покупатели пишут вам напрямую!",
        "📤 Легко! Создать объявление → @Ibaraholka_bot → «+ Подать»\n\nМожно бесплатно (Free) или платно:\n⭐ TOP 50 (наверху 24ч)\n👑 VIP 150 (наверху 7 дней, в 10 раз больше просмотров)\n\nОплата Stars прямо в Telegram. Помощь — пишите мне.",
    ],
    "airpods_question": [
        "🎧 AirPods Pro 2 — есть! 22 000₽, новые запечатанные.\n\nТакже бывают:\n— AirPods 3 — 15 000₽\n— AirPods 2 — 9 000₽\n— AirPods Max — 50 000₽\n\nКакие интересуют?",
        "🎧 Да! У нас всегда AirPods. Напишите модель — подскажу цену и наличие.",
    ],
    "delivery_question": [
        "🚚 Доставка:\n— Самовывоз в Москве — бесплатно (м. Аэропорт)\n— По Москве курьером — 500₽ (СДЭК)\n— По РФ — по тарифам СДЭК / Boxberry\n\nПри встрече проверка товара, всё прозрачно.",
        "📦 Доставляем СДЭКом по всей России. Самовывоз в Москве бесплатно. Покупатель проверяет товар при получении.",
    ],
    "warranty_question": [
        "🛡 Гарантия:\n— Проверка товара при встрече\n— Возврат Stars в течение 7 дней (Telegram)\n— Все устройства проверены перед публикацией\n— Если что-то не так — решим за наш счёт\n\nБезопаснее Авито: тут нельзя подставить фото.",
        "✅ Безопасная сделка через Telegram. Все объявления модерируются. Если возникнут проблемы — возврат Stars.",
    ],
    "thanks": [
        "😊 Рад был помочь! Если будут вопросы — пишите.\n\nОформить покупку → @Ibaraholka_bot",
        "👍 Обращайтесь! Удачной покупки 🍀",
        "🙌 Спасибо! Хорошего дня ✨",
    ],
    "price_negotiation": [
        "💬 По цене — обсуждается! Напишите продавцу напрямую (контакт в объявлении).\n\nОн сам решает, но обычно можно договориться −5-10%.",
        "🤝 Торг уместен, но не больше 10%. Цены и так ниже магазина. Свяжитесь с продавцом.",
    ],
    "fake_check": [
        "🤔 Если хотите проверить оригинальность — при встрече:\n1. Проверка серийника на сайте Apple\n2. Проверка батареи в Настройках\n3. Тест Face ID / Touch ID\n4. Проверка iCloud (чистый ли)\n\nВсё это 2 минуты.",
    ],
    "default": [
        "👋 Я Саша-бот, ассистент АйБарахолки.\n\nМогу подсказать:\n— Цены и наличие\n— Как купить/продать\n— Доставка и гарантии\n— Оформление объявления\n\nПросто напишите вопрос!",
        "🤖 Понял. Если у вас конкретный вопрос — напишите подробнее. Помогу выбрать, оформить, договориться.",
    ],
}


def detect_intent(text: str) -> str:
    """Простой keyword-based intent detection."""
    t = text.lower()
    if any(w in t for w in ['привет', 'здравствуй', 'добрый', 'хай', 'hello', 'hi']):
        return 'greeting'
    if any(w in t for w in ['цена', 'стоит', 'почем', 'price', 'сколько']):
        return 'pricing_question'
    if any(w in t for w in ['продать', 'продаю', 'продам', 'sell']):
        return 'iphone_sell'
    if any(w in t for w in ['купить', 'куплю', 'купи', 'buy', 'ищу', 'хочу']):
        if any(w in t for w in ['airpods', 'наушник']):
            return 'airpods_question'
        return 'iphone_buy'
    if any(w in t for w in ['airpods', 'наушник', 'airpod']):
        return 'airpods_question'
    if any(w in t for w in ['доставк', 'отправ', 'курьер', 'сдэк']):
        return 'delivery_question'
    if any(w in t for w in ['гаранти', 'безопасн', 'верн', 'обман', 'развод']):
        return 'warranty_question'
    if any(w in t for w in ['спасибо', 'благодар', 'thanks']):
        return 'thanks'
    if any(w in t for w in ['торг', 'скидк', 'дешевл', 'брон']):
        return 'price_negotiation'
    if any(w in t for w in ['проверить', 'оригинал', 'подлинник', 'поддел']):
        return 'fake_check'
    return 'default'


def get_script(intent: str) -> str:
    """Получить случайный скрипт для интента."""
    scripts = SALES_SCRIPTS.get(intent, SALES_SCRIPTS['default'])
    return _random.choice(scripts)


@dp.message(F.chat.type == "private")
async def auto_reply(message: types.Message):
    """Бот отвечает на ЛС автоматически по скриптам продаж 24/7."""
    if message.text and message.text.startswith('/'):
        return
    if not message.text:
        return  # стикеры/фото пропускаем

    user_id = message.from_user.id
    username = message.from_user.username or ""
    user_text = message.text

    # 1. Определяем интент
    intent = detect_intent(user_text)

    # 2. Проверяем, есть ли AI-сгенерированный ответ лучше базового
    ai_response = get_learned_response(intent, user_text)
    base_response = get_script(intent)

    # 3. A/B тест: с вероятностью 30% используем AI-ответ, 70% — базовый
    use_ai = ai_response and (hash(user_text) % 10 < 3)
    reply = ai_response if use_ai else base_response

    # 4. Сохраняем разговор для обучения
    save_conversation(user_id, username, user_text, reply, intent, 0 if use_ai else 1)

    # 5. Обновляем профиль клиента
    update_user_profile(user_id, username, message.from_user.first_name or "", intent)

    # 6. Адаптируем стиль под клиента
    style_adaptation = adapt_response_style(reply, user_id)

    # 7. Если это «горячий» лид — добавляем пометку
    is_hot_lead = is_user_hot_lead(user_id)
    lead_note = ""
    if is_hot_lead:
        lead_note = "\n\n🔥 <i>Вижу что вы заинтересованы! @Izdelie0810 подключится в течение 5 минут.</i>"

    try:
        await message.answer(
            f"{style_adaptation}{lead_note}\n\n"
            f"💼 <i>Я — бот-ассистент. Если нужен живой оператор, напишите <b>@Izdelie0810</b>.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Открыть барахолку", web_app=WebAppInfo(url=WEBAPP_URL))],
                [InlineKeyboardButton(text="📢 Канал", url="https://t.me/ibaraholkatyt")],
                [InlineKeyboardButton(text="💬 Написать владельцу", url="https://t.me/Izdelie0810")],
                [InlineKeyboardButton(text="⭐ Это было полезно", callback_data=f"feedback:good:{user_id}")],
            ])
        )
    except Exception as e:
        logging.warning(f"auto_reply error: {e}")


# ============================================================
# САМООБУЧЕНИЕ: AI-бот учится на разговорах
# ============================================================

def save_conversation(user_id: int, username: str, user_msg: str, bot_resp: str,
                      intent: str, variant: int):
    """Сохраняет диалог для последующего обучения."""
    try:
        with db_cursor() as conn:
            conn.execute(
                """INSERT INTO conversations
                   (user_id, username, user_message, bot_response, intent, response_variant, created)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, username, user_msg[:500], bot_resp[:1000], intent, variant, int(time.time()))
            )
            # Обновляем статистику варианта
            conn.execute(
                """INSERT INTO variant_stats (intent, variant_idx, uses, last_updated)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(intent, variant_idx) DO UPDATE SET
                   uses = uses + 1, last_updated = ?""",
                (intent, variant, int(time.time()), int(time.time()))
            )
    except Exception as e:
        logging.warning(f"save_conversation: {e}")


def update_user_profile(user_id: int, username: str, first_name: str, intent: str):
    """Обновляет профиль клиента."""
    try:
        with db_cursor() as conn:
            existing = conn.execute(
                "SELECT messages_count, last_messages FROM user_profiles WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            count = (existing['messages_count'] if existing else 0) + 1
            last_msgs = (existing['last_messages'] if existing else "")[:500]

            # Детектим горячий лид (5+ сообщений за 24ч)
            is_lead = 1 if count >= 5 else 0

            conn.execute(
                """INSERT INTO user_profiles
                   (user_id, username, first_name, preferred_intent, last_messages, messages_count, last_active, is_lead)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   username = excluded.username,
                   first_name = excluded.first_name,
                   preferred_intent = excluded.preferred_intent,
                   last_messages = excluded.last_messages,
                   messages_count = excluded.messages_count,
                   last_active = excluded.last_active,
                   is_lead = excluded.is_lead""",
                (user_id, username, first_name, intent, last_msgs, count, int(time.time()), is_lead)
            )
    except Exception as e:
        logging.warning(f"update_user_profile: {e}")


def get_learned_response(intent: str, user_text: str) -> Optional[str]:
    """Ищет наиболее подходящий AI-сгенерированный ответ."""
    try:
        with db_cursor() as conn:
            # Поиск похожего паттерна
            words = user_text.lower().split()[:5]  # первые 5 слов
            for word in words:
                if len(word) < 3:
                    continue
                row = conn.execute(
                    """SELECT ai_response, confidence FROM ai_responses
                       WHERE intent = ? AND user_pattern LIKE ?
                       ORDER BY confidence DESC, uses DESC LIMIT 1""",
                    (intent, f"%{word}%")
                ).fetchone()
                if row and row['confidence'] > 0.6:
                    return row['ai_response']
        return None
    except Exception as e:
        logging.warning(f"get_learned_response: {e}")
        return None


def adapt_response_style(base: str, user_id: int) -> str:
    """Адаптирует стиль ответа под пользователя."""
    try:
        with db_cursor() as conn:
            prof = conn.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            if not prof:
                return base

            count = prof['messages_count']
            # Если это 2+ сообщение, добавим «помню тебя» эффект
            if count > 1 and count < 5:
                return f"💬 <i>Вижу, мы уже общаемся.</i>\n\n{base}"
            elif count >= 5:
                return f"🤝 <i>Мы уже знакомы! Возможно вам подойдёт особое предложение.</i>\n\n{base}"
            return base
    except Exception as e:
        return base


def is_user_hot_lead(user_id: int) -> bool:
    """Определяет горячий лид (3+ сообщения за час)."""
    try:
        with db_cursor() as conn:
            hour_ago = int(time.time()) - 3600
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM conversations WHERE user_id = ? AND created >= ?",
                (user_id, hour_ago)
            ).fetchone()['c']
            return cnt >= 3
    except Exception:
        return False


# Обработка фидбека (👍/👎)
@dp.callback_query(F.data.startswith("feedback:"))
async def on_feedback(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 3:
        return
    rating_type = parts[1]  # good/bad
    user_id = int(parts[2])

    rating = 1 if rating_type == "good" else -1

    try:
        with db_cursor() as conn:
            # Находим последний диалог с этим юзером
            last = conn.execute(
                "SELECT id, intent, response_variant FROM conversations WHERE user_id = ? ORDER BY created DESC LIMIT 1",
                (user_id,)
            ).fetchone()

            if last:
                conn.execute(
                    "UPDATE conversations SET rating = ? WHERE id = ?",
                    (rating, last['id'])
                )
                # Обновляем статистику
                col = "positive" if rating > 0 else "negative"
                conn.execute(
                    f"""UPDATE variant_stats SET {col} = {col} + 1 WHERE intent = ? AND variant_idx = ?""",
                    (last['intent'], last['response_variant'])
                )

        # Запускаем обучение в фоне
        schedule_learning()

        await callback.answer(
            "👍 Спасибо! Я становлюсь умнее." if rating > 0 else "👎 Понял, буду учиться.",
            show_alert=False
        )
    except Exception as e:
        logging.warning(f"feedback error: {e}")
        await callback.answer("⚠️ Ошибка", show_alert=False)


def schedule_learning():
    """Запускает обучение на последних диалогах (можно через cron)."""
    try:
        with db_cursor() as conn:
            # Берём успешные диалоги за последний час
            hour_ago = int(time.time()) - 3600
            good_convs = conn.execute(
                """SELECT user_message, bot_response, intent, COUNT(*) AS cnt
                   FROM conversations
                   WHERE created >= ? AND rating > 0
                   GROUP BY intent
                   LIMIT 50""",
                (hour_ago,)
            ).fetchall()

            for c in good_convs:
                # Сохраняем как AI-response с высокой уверенностью
                pattern = c['user_message'][:100].lower()
                conn.execute(
                    """INSERT INTO ai_responses
                       (intent, user_pattern, ai_response, confidence, uses, success_rate, created, updated)
                       VALUES (?, ?, ?, 0.7, 0, 0.8, ?, ?)
                       ON CONFLICT(intent, user_pattern) DO UPDATE SET
                       ai_response = excluded.ai_response,
                       confidence = MAX(confidence, 0.7),
                       success_rate = (success_rate + 0.8) / 2,
                       updated = excluded.updated""",
                    (c['intent'], pattern, c['bot_response'], int(time.time()), int(time.time()))
                )

            # Учим негативные — повышаем приоритет базовых скриптов
            bad_convs = conn.execute(
                """SELECT COUNT(*) AS c FROM conversations WHERE created >= ? AND rating < 0""",
                (hour_ago,)
            ).fetchone()
            if bad_convs['c'] > 10:
                # Слишком много плохих — снижаем confidence AI-ответов
                conn.execute(
                    "UPDATE ai_responses SET confidence = MAX(0.5, confidence - 0.1)"
                )
    except Exception as e:
        logging.warning(f"learning error: {e}")


# Команда для просмотра статистики обучения
@dp.message(Command("learn"))
async def cmd_learn(message: types.Message):
    """Показать что бот выучил."""
    if message.from_user.id != 748834052:
        return  # только владельцу
    try:
        with db_cursor() as conn:
            ai_count = conn.execute("SELECT COUNT(*) AS c FROM ai_responses").fetchone()['c']
            conv_count = conn.execute("SELECT COUNT(*) AS c FROM conversations").fetchone()['c']
            good = conn.execute("SELECT COUNT(*) AS c FROM conversations WHERE rating > 0").fetchone()['c']
            bad = conn.execute("SELECT COUNT(*) AS c FROM conversations WHERE rating < 0").fetchone()['c']
            hot_leads = conn.execute("SELECT COUNT(*) AS c FROM user_profiles WHERE is_lead = 1").fetchone()['c']

            top_responses = conn.execute(
                """SELECT intent, COUNT(*) AS c, AVG(rating) AS avg_r
                   FROM conversations
                   WHERE rating IS NOT NULL
                   GROUP BY intent
                   ORDER BY c DESC LIMIT 10"""
            ).fetchall()

            await message.answer(
                f"🧠 <b>Самообучение бота</b>\n\n"
                f"📚 Диалогов в базе: <b>{conv_count}</b>\n"
                f"✅ Положительных: <b>{good}</b>\n"
                f"👎 Отрицательных: <b>{bad}</b>\n"
                f"🤖 AI-сгенерированных ответов: <b>{ai_count}</b>\n"
                f"🔥 Горячих лидов: <b>{hot_leads}</b>\n\n"
                f"📊 <b>Топ интентов по фидбеку:</b>\n" +
                ("\n".join([f"— {r['intent']}: {r['c']} раз, avg rating {r['avg_r']:.1f}" for r in top_responses]) or "<i>нет данных</i>")
            )
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")


@dp.message(Command("paid"))
async def cmd_paid(message: types.Message):
    """DEPRECATED: /paid no longer auto-activates listings (security hole).

    Users must now confirm via the Mini App «✅ Я оплатил — Активировать» button,
    which calls /payments/activate. This command only informs the user.
    """
    await message.answer(
        "⚠️ <b>Команда /paid больше не активирует объявления.</b>\n\n"
        "Эта кнопка была дырой безопасности: любой владелец объявления мог "
        "опубликовать его без реальной оплаты.\n\n"
        "✅ <b>Что делать:</b>\n"
        "1. Оплатите через Telegram Stars, TON или Тинькофф\n"
        "2. Откройте объявление в Mini App\n"
        "3. Нажмите появившуюся кнопку «Активировать»\n\n"
        "Если оплатили, а активация не сработала — пришлите скриншот чека "
        "сюда, активирую вручную."
    )


@dp.message(Command("find"))
async def cmd_find(message: types.Message):
    """Bot-side parser for /find <query> — same parser as /match/subscribe.
    Creates subscription for the user (1st is free, then coins).
    """
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "🔔 <b>Бот-подбиратель</b>\n\n"
            "Опиши что ищешь — буду присылать подходящие объявления в личку.\n\n"
            "<b>Примеры:</b>\n"
            "• /find iPhone 13 до 30К в Москве, чёрный\n"
            "• /find AirPods Pro в идеале\n"
            "• /find iPad 64 ГБ в Спб\n\n"
            "<i>Первая подписка — бесплатно. Дальше 5 монет (посмотри рекламу) или 50 ⭐.</i>"
        )
        return
    raw_query = parts[1].strip()

    user = {"id": message.from_user.id, "first_name": message.from_user.first_name or "", "username": message.from_user.username or ""}

    parsed = _parse_match_query(raw_query)
    if not parsed["keywords"] and not parsed["cat"]:
        await message.answer(
            "❌ Не понял запрос. Укажи товар (iPhone/AirPods/iPad/Mac/Watch) или конкретную модель.\n\n"
            "<b>Примеры:</b>\n"
            "• /find iPhone 13 до 30К в Москве\n"
            "• /find AirPods Pro в идеале"
        )
        return

    now = int(time.time())
    user_id = int(user["id"])
    try:
        with db_cursor() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c, SUM(is_free) AS f FROM match_subscriptions WHERE user_id=? AND active=1",
                (user_id,),
            ).fetchone()
            def _gcnt(r, k, idx):
                if r is None: return 0
                if isinstance(r, dict): return r.get(k) or 0
                try: return r[k]
                except (KeyError, IndexError): return r[idx] if idx < len(r) else 0
            cnt = int(_gcnt(row, "c", 0) or 0)
            free_cnt = int(_gcnt(row, "f", 1) or 0)
    except Exception:
        cnt = 0
        free_cnt = 0

    is_free = (cnt == 0)
    sub_id = "MS-" + _uuid4().hex[:6].upper()

    if is_free:
        try:
            with db_cursor() as conn:
                conn.execute(
                    "INSERT INTO match_subscriptions "
                    "(id, user_id, user_name, user_username, query, keywords, cat, max_price_rub, city, color, extra, active, is_free, paid_until, created, last_notified) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,1,NULL,?,NULL)",
                    (sub_id, user_id, user.get("first_name", ""), user.get("username", ""),
                     raw_query, ",".join(parsed["keywords"]), parsed["cat"], parsed["max_price"],
                     parsed["city"], parsed["color"], parsed["extra"], now),
                )
                conn.commit()
        except Exception as e:
            await message.answer(f"❌ Ошибка создания подписки: {e}")
            return

        lines = []
        if parsed["cat"]: lines.append(f"📦 Категория: {parsed['cat']}")
        if parsed["max_price"]: lines.append(f"💰 До {parsed['max_price']:,} ₽".replace(",", " "))
        if parsed["city"]: lines.append(f"📍 Город: {parsed['city']}")
        if parsed["color"]: lines.append(f"🎨 Цвет: {parsed['color']}")
        if parsed["keywords"]:
            kw_display = ", ".join(parsed["keywords"][:8])
            if len(parsed["keywords"]) > 8: kw_display += f" +{len(parsed['keywords'])-8}"
            lines.append(f"🔍 Ключевые слова: {kw_display}")
        summary = "\n".join(lines) if lines else "—"

        await message.answer(
            f"✅ <b>Подписка создана бесплатно!</b>\n"
            f"ID: <code>{sub_id}</code>\n\n"
            f"{summary}\n\n"
            f"Как только появится объявление по твоему запросу — пришлю в личку.\n\n"
            f"Управлять: /mysubs · отключить: /stop {sub_id}"
        )
    else:
        await message.answer(
            f"💎 <b>У тебя уже есть бесплатная подписка</b>.\n\n"
            f"Дополнительная подписка:\n"
            f"• <b>5 монет</b> в месяц — посмотри рекламу в Mini App и оплати\n"
            f"• <b>50 Stars</b> через Telegram Stars\n\n"
            f"Открой Mini App → «🔔 Подобрать за меня» → укажи запрос и выбери способ оплаты."
        )


@dp.message(Command("mysubs"))
async def cmd_mysubs(message: types.Message):
    user_id = message.from_user.id
    try:
        with db_cursor() as conn:
            rows = conn.execute(
                "SELECT id, query, cat, max_price_rub, city, color, is_free, paid_until, created FROM match_subscriptions WHERE user_id=? AND active=1 ORDER BY created DESC",
                (user_id,),
            ).fetchall()
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    if not rows:
        await message.answer(
            "🔔 У тебя нет активных подписок на подбор.\n\n"
            "Создай: /find iPhone 13 до 30К в Москве"
        )
        return
    lines = ["🔔 <b>Твои подписки:</b>\n"]
    for r in rows:
        def _g(r, k, idx):
            try:
                if hasattr(r, "keys"): return r[k]
                return r[idx]
            except: return None
        sid = _g(r, "id", 0); q = _g(r, "query", 1); cat = _g(r, "cat", 2); maxp = _g(r, "max_price_rub", 3)
        city = _g(r, "city", 4); color = _g(r, "color", 5); is_free = _g(r, "is_free", 6)
        tags = []
        if cat: tags.append(cat)
        if maxp: tags.append(f"до {int(maxp):,} ₽".replace(",", " "))
        if city: tags.append(city)
        if color: tags.append(color)
        tag_str = ", ".join(tags) if tags else "любые"
        type_str = "🆓" if is_free else "💎"
        lines.append(f"{type_str} <code>{sid}</code> — <i>{q}</i>\n   {tag_str}")
    lines.append(f"\nОтключить: /stop MS-XXXXXX")
    await message.answer("\n".join(lines))


@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Укажи ID подписки: /stop MS-XXXXXX")
        return
    sub_id = parts[1].strip().upper()
    if not sub_id.startswith("MS-"):
        sub_id = "MS-" + sub_id
    user_id = message.from_user.id
    try:
        with db_cursor() as conn:
            row = conn.execute("SELECT user_id FROM match_subscriptions WHERE id=?", (sub_id,)).fetchone()
            if not row:
                await message.answer(f"❌ Подписка <code>{sub_id}</code> не найдена.")
                return
            owner = int(row["user_id"]) if hasattr(row, "keys") else int(row[0])
            if owner != user_id:
                await message.answer("❌ Это не твоя подписка.")
                return
            conn.execute("UPDATE match_subscriptions SET active=0 WHERE id=?", (sub_id,))
            conn.commit()
        await message.answer(f"✅ Подписка <code>{sub_id}</code> отключена.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@dp.callback_query(lambda c: c.data and c.data.startswith("match_stop:"))
async def cb_match_stop(call: types.CallbackQuery):
    sub_id = call.data.split(":", 1)[1]
    try:
        with db_cursor() as conn:
            row = conn.execute("SELECT user_id FROM match_subscriptions WHERE id=?", (sub_id,)).fetchone()
            if not row:
                await call.answer("Подписка уже отключена", show_alert=True)
                return
            owner = int(row["user_id"]) if hasattr(row, "keys") else int(row[0])
            if owner != call.from_user.id:
                await call.answer("Это не твоя подписка", show_alert=True)
                return
            conn.execute("UPDATE match_subscriptions SET active=0 WHERE id=?", (sub_id,))
            conn.commit()
        await call.answer("Отключено ✅", show_alert=False)
        if call.message:
            try:
                await call.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
    except Exception as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and ADMIN_IDS:
        return
    with db_cursor() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM listings").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) c FROM listings WHERE status='active'").fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM listings WHERE status='pending' OR status='awaiting_payment'").fetchone()["c"]
        premium = conn.execute("SELECT COUNT(*) c FROM listings WHERE tier IN ('premium','vip') AND status='active'").fetchone()["c"]
        users = conn.execute("SELECT COUNT(DISTINCT user_id) c FROM listings").fetchone()["c"]
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Всего объявлений: <b>{total}</b>\n"
        f"Активных: <b>{active}</b>\n"
        f"Ожидают модерации/оплаты: <b>{pending}</b>\n"
        f"Платных (TOP/VIP): <b>{premium}</b>\n"
        f"Уникальных пользователей: <b>{users}</b>"
    )


@dp.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    # Always accept for our flow (we already validated the listing)
    await bot.answer_pre_checkout_query(query.id, ok=True)


@dp.callback_query(F.data.startswith("confirm_paid:"))
async def on_confirm_paid(callback: types.CallbackQuery):
    """DEPRECATED: ручное подтверждение оплаты убрано во избежание обхода.

    Активация paid listing теперь происходит ТОЛЬКО через:
    1. successful_payment (Telegram Stars) — автоматически
    2. /payments/ton/verify — после проверки on-chain перевода
    3. /admin/listings/{id}/approve — только админ

    Если кнопка всё-таки нажата — объясняем пользователю что делать.
    """
    listing_id = callback.data.split(":", 1)[1]
    await callback.answer("Кнопка устарела", show_alert=True)
    try:
        await callback.message.edit_text(
            f"⚠️ <b>Эта кнопка больше не работает</b>\n\n"
            f"Объявление <code>{listing_id}</code>:\n"
            f"• Если оплачивали через <b>Telegram Stars</b> — оно активируется "
            f"автоматически за пару секунд.\n"
            f"• Если оплачивали через <b>Тинькофф</b> — пришлите боту скриншот "
            f"чека, я активирую вручную.\n"
            f"• Если оплачивали через <b>TON</b> — откройте Mini App и нажмите "
            f"«✅ Я оплатил — проверить» в окне оплаты."
        )
    except Exception:
        pass


@dp.message(F.successful_payment)
async def success_payment(message: types.Message):
    payload = json.loads(message.successful_payment.invoice_payload or "{}")
    listing_id = payload.get("listing_id")
    tier = payload.get("tier", "")

    # Notify user with nice confirmation
    try:
        tier_name = "TOP" if tier == "premium" else "VIP"
        await message.answer(
            f"✅ <b>Оплата прошла!</b>\n\n"
            f"Объявление <code>{listing_id}</code> активировано как <b>{tier_name}</b>.\n\n"
            f"💰 Списано: {message.successful_payment.total_amount}⭐\n\n"
            f"Сейчас оно появится в ленте и в канале @ibaraholkatyt.\n\n"
            f"Нажми кнопку, чтобы открыть барахолку:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Открыть барахолку", web_app=WebAppInfo(url=WEBAPP_URL))]
            ]),
        )
    except Exception as e:
        logger.error(f"Failed to send payment confirmation: {e}")

    if listing_id:
        # Activate in DB
        with db_cursor() as conn:
            conn.execute("UPDATE listings SET status='active' WHERE id=?", (listing_id,))
            conn.commit()

        # Post to channel (paid listings always go to channel)
        try:
            row = None
            with db_cursor() as conn:
                row = conn.execute(
                    "SELECT * FROM listings WHERE id=?", (listing_id,)
                ).fetchone()
            if row:
                item = ListingIn(
                    title=row["title"],
                    description=row["description"],
                    price=row["price"],
                    cat=row["cat"],
                    type=row["type"],
                    contact=row["contact"],
                    photo=row["photo"],
                    tier=row["tier"],
                    city=row["city"],
                )
                user_dict = {
                    "id": row["user_id"],
                    "first_name": row["user_name"],
                    "username": row["user_username"],
                }
                await post_to_channel(listing_id, item, user_dict)
        except Exception as e:
            logger.error(f"Failed to post paid listing {listing_id} to channel: {e}")

        logger.info(f"Listing {listing_id} activated (paid by user {message.from_user.id})")

    # Notify admins
    if ADMIN_IDS:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"💰 <b>Оплата</b>\n\n"
                    f"Пользователь: {message.from_user.first_name} ({message.from_user.id})\n"
                    f"Объявление: {listing_id}\n"
                    f"Тариф: {tier}\n"
                    f"Сумма: {message.successful_payment.total_amount}⭐",
                )
            except Exception:
                pass

    await message.answer(
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"Твоё объявление опубликовано как <b>{TIER_LABELS.get(tier, tier)}</b>.\n\n"
        f"Можешь проверить в барахолке 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Открыть барахолку", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]),
    )


# ============================================================
# Channel posting
# ============================================================
CAT_LABELS = {"iphone": "iPhone", "airpods": "AirPods", "ipad": "iPad", "mac": "Mac", "watch": "Watch", "accs": "Аксессуары"}
TYPE_LABELS = {"sell": "Продам", "buy": "Куплю", "exchange": "Обмен", "opt": "Опт"}


def format_listing_for_channel(item: ListingIn, user: Dict[str, Any]) -> tuple[str, types.InlineKeyboardMarkup | None]:
    """Build message text + inline button to publish to channel."""
    cat_label = CAT_LABELS.get(item.cat, item.cat)
    type_label = TYPE_LABELS.get(item.type, item.type)
    tier_label = "🌟 TOP" if item.tier == "premium" else "👑 VIP"

    price_line = ""
    if item.type == "buy":
        price_line = f"\n💵 <b>Бюджет:</b> до {item.price:,.0f} ₽".replace(",", " ")
    elif item.price > 0:
        price_line = f"\n💵 <b>Цена:</b> {item.price:,.0f} ₽".replace(",", " ")

    user_link = ""
    if user.get("username"):
        user_link = f"https://t.me/{user['username']}"
    elif user.get("id"):
        user_link = f"tg://user?id={user['id']}"

    text = (
        f"{tier_label} · {cat_label} · {type_label}\n\n"
        f"<b>{item.title}</b>"
        f"{price_line}\n\n"
        f"{item.description}\n\n"
        f"📍 {item.city}\n"
        f"👤 {user.get('first_name', 'Продавец')}\n"
        f"{'🔗 Открыть в барахолке: ' + WEBAPP_URL if WEBAPP_URL else ''}"
    ).strip()

    keyboard = None
    if user_link:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📩 Написать продавцу", url=user_link)]
        ])

    return text, keyboard


async def post_to_channel(listing_id: str, item: ListingIn, user: Dict[str, Any]) -> Optional[int]:
    """Post a listing to the configured channel. Returns message_id if posted.

    Uses direct HTTP call to Telegram API to bypass any aiogram session issues.

    Hard guard: paid-tier listings (premium / vip) only get posted if the
    database row says status='active'. Free listings always pass.
    """
    if not BOT_TOKEN or not CHANNEL_ID:
        print(f"[POST_CHANNEL] {listing_id}: SKIPPED - missing BOT_TOKEN or CHANNEL_ID", flush=True)
        return None

    # ===== Payment-status gate =====
    tier = (item.tier or "free").lower()
    if tier in ("premium", "vip"):
        try:
            with db_cursor() as conn:
                row = conn.execute(
                    "SELECT status, channel_message_id FROM listings WHERE id=?",
                    (listing_id,),
                ).fetchone()
            if not row or row["status"] != "active":
                print(
                    f"[POST_CHANNEL] {listing_id}: BLOCKED — paid tier '{tier}' "
                    f"but status={row['status'] if row else 'missing'}, payment not confirmed",
                    flush=True,
                )
                # If there's a stale channel post for this unpaid listing,
                # try to clean it up too.
                stale = row["channel_message_id"] if row else None
                if stale:
                    try:
                        await delete_from_channel(stale)
                        with db_cursor() as conn:
                            conn.execute(
                                "UPDATE listings SET channel_message_id=NULL WHERE id=?",
                                (listing_id,),
                            )
                            conn.commit()
                    except Exception:
                        pass
                return None
        except Exception as e:
            print(f"[POST_CHANNEL] {listing_id}: gate check error: {e}", flush=True)
            return None
    # ===============================
    try:
        # Build simple text directly (bypass format_listing_for_channel for now)
        price_str = f"{item.price:,} ₽".replace(",", " ")
        tier_emoji = "👑" if item.tier == "vip" else ("⭐" if item.tier == "premium" else "📦")
        tier_label = {"vip": "VIP", "premium": "TOP", "free": ""}.get(item.tier, "")
        tier_part = f"{tier_emoji} {tier_label} · " if tier_label else ""
        text = (
            f"{tier_part}{item.cat.capitalize()} · Продам\n\n"
            f"<b>{item.title}</b>\n"
            f"💰 Цена: {price_str}\n\n"
            f"📍 {item.city}\n"
            f"👤 {user.get('first_name', 'Продавец')}\n"
            f"🔗 Открыть в барахолке:\n"
            f"https://ibaraholka.p.spru.io/"
        )
        print(f"[POST_CHANNEL] {listing_id}: text built, length={len(text)}", flush=True)

        import urllib.request
        import urllib.parse
        payload = {
            "chat_id": str(CHANNEL_ID),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        print(f"[POST_CHANNEL] {listing_id}: Telegram ok={result.get('ok')}", flush=True)
        if result.get("ok"):
            msg_id = result["result"]["message_id"]
            with db_cursor() as conn:
                conn.execute("UPDATE listings SET channel_message_id=? WHERE id=?", (msg_id, listing_id))
                conn.commit()
            print(f"[POST_CHANNEL] {listing_id}: SAVED msg_id={msg_id}", flush=True)
            # Fire-and-forget: notify match-subscribers (does not block post)
            try:
                await _notify_match_subscribers(listing_id, item, user, msg_id)
            except Exception as e:
                logger.warning(f"match_notify failed for {listing_id}: {e}")
            return msg_id
        else:
            print(f"[POST_CHANNEL] {listing_id}: API error: {result}", flush=True)
            return None
    except Exception as e:
        print(f"[POST_CHANNEL] {listing_id}: EXCEPTION: {type(e).__name__}: {e}", flush=True)
        return None


async def delete_from_channel(channel_message_id: Optional[int]) -> bool:
    """Delete a message from the channel via direct HTTP call. Returns True if deleted."""
    if not BOT_TOKEN or not CHANNEL_ID or channel_message_id is None:
        return False
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({
            "chat_id": str(CHANNEL_ID),
            "message_id": str(channel_message_id),
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        if result.get("ok"):
            logger.info(f"Deleted channel message {channel_message_id}")
            return True
        return False
    except Exception as e:
        logger.warning(f"Failed to delete channel message {channel_message_id}: {e}")
        return False


# ============================================================
# FastAPI app
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("✅ DB initialized")
    yield


app = FastAPI(title="АйБарахолка API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GZip compression for responses >= 500 bytes — cuts JSON payload ~70% (3.5KB → 1KB)
app.add_middleware(GZipMiddleware, minimum_size=500)


# === Mini App static serving (v24.html + картинки) ===
# Раздаёт Mini App с того же домена что и API — нет CORS, нет watermark "Made with Spru"
import pathlib
MINIAPP_DIR = pathlib.Path(__file__).parent / "miniapp"

@app.get("/mini", response_class=HTMLResponse)
@app.get("/mini/", response_class=HTMLResponse)
async def mini_app():
    """Отдаёт index.html Mini App"""
    p = MINIAPP_DIR / "index.html"
    if not p.exists():
        return HTMLResponse(content="<h1>Mini App not deployed</h1>", status_code=404)
    return HTMLResponse(content=p.read_text(encoding="utf-8"), headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.get("/mini/{filename}")
async def mini_static(filename: str):
    from fastapi.responses import HTMLResponse, FileResponse
    p = MINIAPP_DIR / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/")
def root():
    return {"app": "АйБарахолка API", "version": "1.0.0", "status": "ok"}


@app.get("/health")
def health():
    """Health check that also keeps the DB connection pool warm.
    Without this, the first request after a quiet period would pay the
    Neon TCP+TLS+auth handshake (~300ms). With it, the pool stays primed.
    """
    try:
        with db_cursor() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        pass  # health check never fails on DB
    return {"ok": True, "ts": int(datetime.now().timestamp())}


@app.get("/debug/logs")
def debug_logs():
    """Debug endpoint: show last_post.log if available."""
    try:
        with open("/data/last_post.log", "r") as f:
            return {"ok": True, "log": f.read()}
    except FileNotFoundError:
        return {"ok": False, "error": "No log file yet"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/debug/test-auth")
def debug_test_auth(authorization: str = Header(None)):
    """Debug endpoint: test what get_user returns for given Authorization header."""
    import asyncio
    user = asyncio.run(_get_user_sync(authorization or ""))
    return {
        "ok": True,
        "authorization_present": bool(authorization),
        "authorization_prefix": (authorization[:20] + "...") if authorization else None,
        "user": user,
        "DEMO_MODE": DEMO_MODE,
        "is_demo_user": user.get("_demo", False) if isinstance(user, dict) else False,
    }


async def _get_user_sync(authorization: str) -> dict:
    """Async helper for /debug/test-auth."""
    from fastapi import HTTPException as HTTPExc
    try:
        if not authorization or not authorization.startswith("tma "):
            if DEMO_MODE:
                return {"id": 999999, "first_name": "Demo", "username": "Izdelie0810", "_demo": True}
            raise HTTPExc(401, "Authorization required")
        raw = authorization[4:]
        try:
            return validate_init_data(raw)
        except HTTPExc:
            # Try to extract user from any unverified initData (e.g. hash=unsupported_domain)
            try:
                import urllib.parse as _up
                params = dict(_up.parse_qsl(raw, keep_blank_values=True))
                user_json = params.get("user")
                if user_json:
                    u = json.loads(user_json)
                    user_id = int(u.get("id", 0))
                    if user_id > 0:
                        return {"id": user_id, "first_name": u.get("first_name"), "username": u.get("username"), "_unverified": True}
            except Exception:
                pass
            raise
    except HTTPExc as e:
        return {"error": str(e.detail), "status_code": e.status_code}


@app.get("/debug/state")
def debug_state():
    """Debug endpoint: show env vars + DB state."""
    import os
    state = {
        "BOT_TOKEN_set": bool(os.getenv("BOT_TOKEN")),
        "BOT_TOKEN_prefix": os.getenv("BOT_TOKEN", "")[:15] + "...",
        "CHANNEL_ID": os.getenv("CHANNEL_ID") or CHANNEL_ID,  # use module default if env var missing
        "WEBAPP_URL": WEBAPP_URL,
        "DEMO_MODE_env": os.getenv("DEMO_MODE"),
        "DEMO_MODE_module": DEMO_MODE,
        "ADMIN_TOKEN_set": bool(ADMIN_TOKEN),
        "PORT": os.getenv("PORT"),
        "DB_PATH_env": os.getenv("DB_PATH"),
        "DB_FILE": DB_FILE,
        "DATABASE_URL_set": bool(os.getenv("DATABASE_URL")),
        "DATABASE_URL_prefix": (os.getenv("DATABASE_URL", "")[:30] + "...") if os.getenv("DATABASE_URL") else "EMPTY",
        "USE_POSTGRES": USE_POSTGRES,
        "db_kind": "postgres" if USE_POSTGRES else "sqlite",
        "/data_exists": os.path.isdir("/data"),
        "/data_writable": os.access("/data", os.W_OK) if os.path.isdir("/data") else False,
        "module_BOT_TOKEN": bool(BOT_TOKEN),
        "module_BOT_TOKEN_prefix": BOT_TOKEN[:15] + "..." if BOT_TOKEN else "EMPTY",
        "module_CHANNEL_ID": CHANNEL_ID,
        "bot_initialized": bot is not None,
        "dp_initialized": dp is not None,
        "railway_git_commit_sha": (os.getenv("RENDER_GIT_COMMIT_SHA") or os.getenv("RAILWAY_GIT_COMMIT_SHA") or "N/A")[:8],
        "render_service_id": os.getenv("RENDER_SERVICE_ID", "N/A"),
        "render_external_url": os.getenv("RENDER_EXTERNAL_URL", "N/A"),
    }
    try:
        with db_cursor() as conn:
            n = conn.execute("SELECT COUNT(*) as cnt FROM listings").fetchone()
            state["listings_count"] = n["cnt"]
            last = conn.execute("SELECT id, tier, status, channel_message_id FROM listings ORDER BY created DESC LIMIT 5").fetchall()
            state["last_listings"] = [
                {"id": r["id"], "tier": r["tier"], "status": r["status"], "ch_msg": r["channel_message_id"]}
                for r in last
            ]
    except Exception as e:
        state["db_error"] = str(e)
    # Schema check
    try:
        with db_cursor() as conn:
            # PRAGMA works only in SQLite. For Postgres we use information_schema.
            try:
                cols = conn.execute("PRAGMA table_info(listings)").fetchall()
                state["listings_columns"] = [r[1] for r in cols]
            except Exception:
                cols = conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='listings'"
                ).fetchall()
                # PG returns tuples, sqlite returns Row objects; handle both
                col_names = []
                for c in cols:
                    try:
                        col_names.append(c['column_name'])
                    except Exception:
                        col_names.append(c[0])
                state["listings_columns"] = col_names
            state["has_channel_message_id"] = "channel_message_id" in state["listings_columns"]
    except Exception as e:
        state["schema_error"] = str(e)
    return state


@app.get("/debug/version")
def debug_version():
    """Show which commit is actually deployed (helps detect stale builds)."""
    import os, subprocess
    info = {
        "render_git_sha": (os.getenv("RENDER_GIT_COMMIT_SHA") or "N/A")[:12],
        "railway_git_sha": (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "N/A")[:12],
        "module_sha": "unknown",
    }
    # Try to detect from module source — find sentinel comment
    try:
        src_path = os.path.abspath(__file__)
        with open(src_path, "r") as f:
            content = f.read()
        # find deploy trigger sentinel
        import re
        m = re.search(r"# deploy-trigger (\d+)", content)
        if m:
            info["module_sha"] = f"trigger-{m.group(1)}"
        # last main.py commit
        try:
            out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=os.path.dirname(src_path), stderr=subprocess.DEVNULL).decode().strip()
            info["local_git_sha"] = out
        except Exception:
            pass
    except Exception as e:
        info["error"] = str(e)
    return info


@app.get("/listings")
def list_listings(
    cat: Optional[str] = Query(None, pattern="^(iphone|airpods|ipad|mac|watch|accs)$"),
    type: Optional[str] = Query(None, pattern="^(sell|buy|exchange|opt)$"),
    since: Optional[str] = Query(None, pattern="^(1h|24h|7d)$"),
    limit: int = Query(100, ge=1, le=500),
):
    """Public list of active listings (sorted by tier then recency).

    Query params:
      cat — filter by category
      type — sell/buy/exchange/opt
      since — 1h | 24h | 7d (filter by created timestamp)
      limit — max results (default 100)
    """
    now = int(datetime.now().timestamp())
    since_seconds = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}.get(since, 0)
    since_ts = now - since_seconds if since_seconds else None

    with db_cursor() as conn:
        q = (
            "SELECT id, user_id, user_name, user_username, title, description, price, cat, type, "
            "contact, photo, tier, city, status, created, expires_at "
            "FROM listings "
            "WHERE status='active' AND (expires_at IS NULL OR expires_at > ?)"
        )
        params: List = [now]
        if cat:
            q += " AND cat=?"
            params.append(cat)
        if type:
            q += " AND type=?"
            params.append(type)
        if since_ts is not None:
            q += " AND created>=?"
            params.append(since_ts)
        q += (
            " ORDER BY CASE tier WHEN 'vip' THEN 0 WHEN 'premium' THEN 1 ELSE 2 END, "
            "created DESC LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(q, params).fetchall()

        # Build ETag from row count + max created (cheap and stable for our poll cadence)
        row_count = len(rows)
        max_created = max((r["created"] or 0) for r in rows) if rows else 0
        etag = f'W/"r{row_count}-m{max_created}-c{cat or "x"}-s{since or "x"}-t{type or "x"}-l{limit}"'

        from fastapi import Response
        resp = Response(
            content=json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str),
            media_type="application/json",
        )
        resp.headers["Cache-Control"] = "public, max-age=10"
        resp.headers["ETag"] = etag
        resp.headers["X-Result-Count"] = str(row_count)
        return resp


@app.get("/listings/{listing_id}/status")
def listing_status(listing_id: str):
    """Lightweight status endpoint for Mini App to poll after payment.

    Returns tier/status/created/ch_msg so the Mini App can show a green
    checkmark and close the pay-modal the moment the listing is active.
    """
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT id, status, tier, created, channel_message_id FROM listings WHERE id=?",
            (listing_id,),
        ).fetchone()
    if not row:
        return {"ok": False, "error": "not_found"}
    return {
        "ok": True,
        "id": row["id"],
        "status": row["status"],
        "tier": row["tier"],
        "created": row["created"],
        "channel_message_id": row["channel_message_id"],
        "is_active": row["status"] == "active",
    }


@app.post("/listings")
async def create_listing(item: ListingIn, request: Request):
    """Create new listing. Requires Telegram WebApp Authorization.

    Admin bypass: X-Admin-Token header allows creating listings without Telegram auth.
    """
    print(f"[CREATE_LISTING] Start: tier={item.tier}, title={item.title}", flush=True)

    # Check for admin bypass FIRST (before user resolution)
    admin_token = request.headers.get("x-admin-token", "")
    is_admin = admin_token == ADMIN_TOKEN and bool(ADMIN_TOKEN)

    if is_admin:
        # Admin: synthesize user from request body or use placeholder
        user = {
            "id": request.headers.get("x-admin-user-id", 8925325612),  # Default to Sasha's ID
            "first_name": request.headers.get("x-admin-user-name", "Admin"),
            "username": request.headers.get("x-admin-user-username", "admin"),
            "_admin": True,
        }
        is_demo_user = False  # Admin acts as a real user for invoice purposes
    else:
        user = await get_user(request.headers.get("authorization", ""))
        is_demo_user = user.get("_demo", False)

    print(f"[CREATE_LISTING] User: id={user.get('id')}, demo={is_demo_user}, admin={is_admin}, name={user.get('first_name')}", flush=True)
    try:
        with open("/data/last_post.log", "a") as f:
            f.write(f"[CREATE_LISTING] User: id={user.get('id')}, demo={is_demo_user}, admin={is_admin}\n")
    except Exception:
        pass

    listing_id = "l_" + str(int(datetime.now().timestamp() * 1000))

    # Tier expiry
    expires_at = None
    if item.tier in TIER_DURATIONS:
        expires_at = int(datetime.now().timestamp()) + TIER_DURATIONS[item.tier]

    initial_status = "active" if (item.tier == "free" or is_demo_user or is_admin) else "awaiting_payment"

    with db_cursor() as conn:
        conn.execute(
            """INSERT INTO listings
            (id, user_id, user_name, user_username, title, description, price, cat, type,
             contact, photo, tier, city, status, created, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                listing_id,
                user["id"],
                user.get("first_name", ""),
                user.get("username", ""),
                item.title,
                item.description,
                item.price,
                item.cat,
                item.type,
                item.contact,
                item.photo,
                item.tier,
                item.city,
                initial_status,
                int(datetime.now().timestamp() * 1000),
                expires_at,
            ),
        )
        conn.commit()

    logger.info(
        f"Listing {listing_id} created: user={user['id']} tier={item.tier} "
        f"status={initial_status} demo={is_demo_user}"
    )

    # Posting to channel happens inside the invoice block above
    # (so demo users get channel posts without invoice, real users get channel post after payment)

    # Paid tier → send Stars invoice via direct HTTP (reliable)
    invoice_msg_id = None
    invoice_error = None
    skip_invoice_reason = None

    # v57: НЕ шлём Stars-инвойс автоматически из листинга — Mini App сам откроет Т-Банк
    # Если нужен Stars — пользователь может перейти по /start=pay_<listing>_<tier>
    skip_invoice_reason = "v57: Stars invoice is not sent automatically from /listings endpoint. Mini App opens Tinkoff directly."
    print(f"[INVOICE] {listing_id}: skipped (v57: use pay_ deep-link or Tinkoff)", flush=True)

    # Post to channel ONLY if listing status is already 'active'.
    # - Free / demo / admin → status set to 'active' on creation → post immediately
    # - Real user paid tier → status='awaiting_payment' here → SKIP,
    #   will be posted later by successful_payment / confirm_paid / cmd_paid
    if initial_status == "active":
        log_msg = f"[CREATE_LISTING] {listing_id}: ENTERING channel post block, tier={item.tier} status={initial_status}"
        print(log_msg, flush=True)
        try:
            with open("/data/last_post.log", "a") as f:
                f.write(log_msg + "\n")
        except Exception:
            pass
        # Inline direct HTTP post (proven to work via /debug/post-channel-test)
        try:
            price_str = f"{item.price:,} ₽".replace(",", " ")
            tier_emoji = "👑" if item.tier == "vip" else ("⭐" if item.tier == "premium" else "📦")
            text = (
                f"{tier_emoji} {item.cat.capitalize()} · Продам\n\n"
                f"<b>{item.title}</b>\n"
                f"💰 Цена: {price_str}\n\n"
                f"📍 {item.city}\n"
                f"🔗 https://ibaraholka.p.spru.io/"
            )
            import urllib.request
            import urllib.parse
            payload = {
                "chat_id": str(CHANNEL_ID),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
            data = urllib.parse.urlencode(payload).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data=data,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
            if result.get("ok"):
                msg_id = result["result"]["message_id"]
                log_msg = f"[CREATE_LISTING] {listing_id}: SUCCESS msg_id={msg_id}"
                print(log_msg, flush=True)
                try:
                    with open("/data/last_post.log", "a") as f:
                        f.write(log_msg + "\n")
                except Exception:
                    pass
                with db_cursor() as conn:
                    cur = conn.execute(
                        "UPDATE listings SET channel_message_id=? WHERE id=?",
                        (msg_id, listing_id),
                    )
                    conn.commit()
                    log_msg = f"[CREATE_LISTING] {listing_id}: DB UPDATED rows={cur.rowcount}"
                    print(log_msg, flush=True)
                    try:
                        with open("/data/last_post.log", "a") as f:
                            f.write(log_msg + "\n")
                    except Exception:
                        pass
            else:
                log_msg = f"[CREATE_LISTING] {listing_id}: TG error: {result}"
                print(log_msg, flush=True)
                try:
                    with open("/data/last_post.log", "a") as f:
                        f.write(log_msg + "\n")
                except Exception:
                    pass
        except Exception as e:
            log_msg = f"[CREATE_LISTING] {listing_id}: EXCEPTION {type(e).__name__}: {e}"
            print(log_msg, flush=True)
            try:
                with open("/data/last_post.log", "a") as f:
                    f.write(log_msg + "\n")
            except Exception:
                pass

    # If invoice failed for paid tier (not demo), downgrade listing to free
    if item.tier in ("premium", "vip") and invoice_error and not is_demo_user:
        with db_cursor() as conn:
            conn.execute("UPDATE listings SET tier='free' WHERE id=?", (listing_id,))
            conn.commit()
        logger.info(f"Listing {listing_id}: downgraded to free due to invoice failure")

    # Send Telegram notification with "I paid" button for paid-tier listings
    if item.tier in ("premium", "vip") and not is_demo_user and not is_admin:
        try:
            tier_name = "TOP 24 часа" if item.tier == "premium" else "VIP 7 дней"
            notify_text = (
                f"✅ <b>Объявление создано!</b>\n\n"
                f"<b>{item.title}</b>\n"
                f"💰 Цена: {item.price:,} ₽\n"
                f"📍 {item.city}\n"
                f"🎯 Тариф: <b>{tier_name}</b>\n"
                f"🆔 ID: <code>{listing_id}</code>\n\n"
                f"<b>Способы оплаты:</b>\n\n"
                f"⭐ <b>Оплатить через Telegram Stars</b> — нажмите кнопку ниже "
                f"в сообщении с инвойсом (найдёте его выше).\n\n"
                f"💳 <b>Оплатить через Тинькофф</b> — перейдите по ссылке:\n"
                f"https://www.tbank.ru/rm/r_TGugYbYVEb.mLmrPUwlTy/aHI4Y75190\n\n"
                f"<b>После оплаты через Тинькофф:</b>\n"
                f"После оплаты через Тинькофф пришлите боту скриншот чека — "
                f"активирую объявление вручную (5-10 мин)."
            )
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
            notify_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="💳 Оплатить через Тинькофф",
                    url="https://www.tbank.ru/rm/r_TGugYbYVEb.mLmrPUwlTy/aHI4Y75190"
                )],
                [InlineKeyboardButton(
                    text="📱 Открыть барахолку",
                    web_app=WebAdmin_URL if False else WEBAPP_URL
                )],
            ])
            await bot.send_message(user["id"], notify_text, reply_markup=notify_kb)
            logger.info(f"Listing {listing_id}: paid notification with tbank button sent")
        except Exception as e:
            logger.error(f"Paid notification error for {listing_id}: {e}")

    return {
        "id": listing_id,
        "status": "active",
        "tier": ("free" if (invoice_error and not is_demo_user) else item.tier),
        "invoice_sent": invoice_msg_id is not None,
        "invoice_error": invoice_error,
        "skip_invoice_reason": skip_invoice_reason,
    }


@app.post("/debug/post-channel-test")
async def debug_post_channel_test(request: Request):
    """Debug: try posting a test message to the channel directly."""
    if request.headers.get("x-admin-token", "") != ADMIN_TOKEN:
        raise HTTPException(403, "Admin only")
    try:
        import urllib.request
        import urllib.parse
        payload = {
            "chat_id": str(CHANNEL_ID),
            "text": "🧪 Debug test from /debug/post-channel-test",
            "disable_web_page_preview": "true",
        }
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        return {
            "ok": True,
            "bot_token_set": bool(BOT_TOKEN),
            "channel_id": CHANNEL_ID,
            "telegram_result": result,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "bot_token_set": bool(BOT_TOKEN), "channel_id": CHANNEL_ID}


@app.post("/payments/yukassa/create")
async def create_yukassa_payment(request: Request):
    """Create a YooKassa payment for a listing. Returns confirmation_url.

    Modes:
    - Real (YOOKASSA_SHOP_ID + secret set): creates ЮKassa payment via API,
      returns confirmation_url (ЮKassa-hosted page where user pays card/SBP/...).
    - Test/fallback: returns ready-made ЮMoney wallet payment URLs so the
      integration works even before the shop is fully onboarded.
    """
    body = await request.json()
    listing_id = body.get("listing_id", "")
    tier = body.get("tier", "vip")
    user_id = body.get("user_id", 0)

    if tier not in TIER_PRICES:
        raise HTTPException(400, "Invalid tier")
    amount_rub = TIER_PRICES[tier] * 1.4  # 50⭐=70₽, 150⭐=210₽
    amount_rub_int = int(amount_rub)

    shop_id = os.getenv("YOOKASSA_SHOP_ID", "")
    secret_key = os.getenv("YOOKASSA_SECRET_KEY", "")
    yoowallet = os.getenv("YOOMONEY_WALLET", "")  # 41001... wallet for ЮMoney direct

    # ----- Test / fallback mode: no real ЮKassa keys -----
    if not shop_id or not secret_key:
        # ЮMoney quickpay form requires `receiver` (wallet) to actually accept money.
        # If wallet is set, the URL works for real payments; otherwise it's a demo link.
        quickpay_url = None
        if yoowallet:
            label = f"ib-{listing_id}-{int(datetime.now().timestamp())}"
            quickpay_url = (
                f"https://yoomoney.ru/quickpay/shop.xml"
                f"?receiver={yoowallet}"
                f"&quickpay-form=shop"
                f"&paymentType=AC"
                f"&sum={amount_rub_int}"
                f"&label={label}"
                f"&successURL=https://t.me/Ibaraholka_bot"
                f"&targets=АйБарахолка {tier.upper()} {listing_id}"
            )
        tinkoff_url = (
            f"https://www.tinkoff.ru/rm/r_TGugYbYVEb.mLmrPUwlTy"
            f"?amount={amount_rub_int}00&successURL=https://t.me/Ibaraholka_bot"
        )
        sber_url = (
            f"https://online.sberbank.ru/CSAFront/payment/showPrePaymentPage.do"
            f"?amount={amount_rub_int}&to=АйБарахолка"
        )
        mode = "yoomoney" if quickpay_url else "tinkoff"
        return {
            "ok": True,
            "test_mode": True,
            "fallback": True,
            "mode": mode,
            "amount_rub": amount_rub_int,
            "quickpay_url": quickpay_url,
            "tinkoff_url": tinkoff_url,
            "sber_url": sber_url,
            "has_wallet": bool(yoowallet),
            "instruction": (
                "После оплаты вернись в WebApp и нажми «✅ Я оплатил — активировать» — "
                "объявление появится в канале @ibaraholkatyt после проверки."
            ),
        }

    # ----- Real YooKassa integration -----
    try:
        import urllib.request
        import base64
        import secrets
        idem_key = secrets.token_hex(16)
        auth = base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
        return_url = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/Ibaraholka_bot")
        payload = {
            "amount": {"value": f"{amount_rub_int}.00", "currency": "RUB"},
            "capture": True,
            "confirmation": {
                "type": "redirect",
                "return_url": return_url,
            },
            "description": f"АйБарахолка · {tier.upper()} · {listing_id}",
            "metadata": {"listing_id": listing_id, "tier": tier, "user_id": str(user_id)},
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.yookassa.ru/v3/payments",
            data=data,
            headers={
                "Authorization": f"Basic {auth}",
                "Idempotence-Key": idem_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        return {
            "ok": True,
            "test_mode": False,
            "fallback": False,
            "mode": "yukassa",
            "payment_id": result.get("id"),
            "confirmation_url": result.get("confirmation", {}).get("confirmation_url"),
            "amount_rub": amount_rub_int,
            "instruction": (
                "После оплаты вернись в WebApp и нажми «✅ Я оплатил — активировать» — "
                "объявление появится в канале @ibaraholkatyt после проверки."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/payments/tinkoff/notify")
async def tinkoff_notify(request: Request):
    """User-driven Tinkoff payment confirmation.

    Flow (no merchant API available for solo/self-employed):
      1. User clicks "Оплатить через Тинькофф" → opens Tinkoff payment link
      2. Pays through bank (card/SBP/any method)
      3. Returns to Mini App, presses "✅ Я оплатил"
      4. Backend marks listing as `paid` (NOT active yet — user must confirm)
      5. Mini App shows "✅ Оплата прошла! [Активировать объявление]" button
      6. User clicks → POST /payments/activate → status=active + post to channel

    Two-step flow prevents premature publication: user only sees the
    "published" green modal after they themselves confirm.
    """
    body = await request.json()
    listing_id = body.get("listing_id", "")
    deal_id = body.get("deal_id", "")
    user_id = int(body.get("user_id", 0) or 0)
    tier = body.get("tier", "")

    if not user_id:
        return {"ok": False, "error": "no user_id"}

    # ESCROW DEAL FLOW
    if deal_id:
        try:
            with db_cursor() as conn:
                row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
                if not row:
                    return {"ok": False, "error": "deal_not_found"}
                d = _deal_row_to_dict(row)
                if d["buyer_id"] != user_id:
                    return {"ok": False, "error": "not_buyer"}
                if d["status"] not in ("awaiting_payment", "escrowed"):
                    return {"ok": False, "error": f"bad_status:{d['status']}"}
                if d["status"] == "escrowed":
                    return {"ok": True, "status": "escrowed", "deal_id": deal_id,
                            "instruction": "Деньги в гаранте. Продавец скоро отправит."}
                conn.execute(
                    "UPDATE deals SET status='escrowed', paid_at=? WHERE id=?",
                    (int(time.time()), deal_id),
                )
                conn.commit()
            logger.info(f"DEAL_ESCROWED deal={deal_id} user={user_id}")
            return {"ok": True, "status": "escrowed", "deal_id": deal_id,
                    "instruction": "Деньги в гаранте. Продавец скоро отправит."}
        except Exception as e:
            logger.exception(f"tinkoff_notify deal error: {e}")
            return {"ok": False, "error": f"deal_path: {e}"}

    if not listing_id:
        return {"ok": False, "error": "no listing_id"}

    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "listing_not_found"}
        if row["user_id"] != user_id:
            return {"ok": False, "error": "not_owner"}
        if row["status"] not in ("awaiting_payment", "paid"):
            return {"ok": False, "error": f"bad_status:{row['status']}"}
        # Already paid? Just return state.
        if row["status"] == "paid":
            return {
                "ok": True,
                "status": "paid",
                "listing_id": listing_id,
                "instruction": "Оплата зафиксирована. Нажмите «Активировать объявление» в Mini App.",
            }
        # Mark as paid (NOT active yet — user must explicitly activate)
        conn.execute(
            "UPDATE listings SET status='paid', paid_at=extract(epoch from now())::bigint WHERE id=?",
            (listing_id,),
        )
        conn.commit()

    # Audit log + admin heads-up
    try:
        logger.info(f"PAYMENT_PAID listing={listing_id} user={user_id} tier={tier} method=Tinkoff")
    except Exception:
        pass

    return {
        "ok": True,
        "status": "paid",
        "listing_id": listing_id,
        "instruction": "Оплата зафиксирована. Нажмите «Активировать объявление» в Mini App.",
    }


@app.post("/payments/activate")
async def payments_activate(request: Request):
    """Step 2: user confirms publication after seeing payment deducted.

    Used by all 3 methods (Tinkoff / Stars / TON) — backend checks status='paid'
    and only then flips to active + posts to channel.
    """
    body = await request.json()
    listing_id = body.get("listing_id", "")
    user_id = int(body.get("user_id", 0) or 0)

    if not listing_id:
        return {"ok": False, "error": "no listing_id"}
    if not user_id:
        return {"ok": False, "error": "no user_id"}

    item_dict = None
    owner_info = None
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)
        ).fetchone()
        if not row:
            return {"ok": False, "error": "listing_not_found"}
        if row["user_id"] != user_id:
            return {"ok": False, "error": "not_owner"}
        # Already active? Return current state (idempotent).
        if row["status"] == "active":
            return {
                "ok": True,
                "activated": listing_id,
                "already_active": True,
                "channel_message_id": row["channel_message_id"],
            }
        if row["status"] != "paid":
            return {"ok": False, "error": f"bad_status:{row['status']} (нужно сначала оплатить)"}

        item_dict = {
            "id": row["id"], "title": row["title"], "description": row["description"],
            "price": row["price"], "cat": row["cat"], "type": row["type"],
            "contact": row["contact"], "photo": row["photo"], "tier": row["tier"],
            "city": row["city"],
        }
        owner_info = {
            "id": row["user_id"], "first_name": row["user_name"], "username": row["user_username"]
        }

        conn.execute("UPDATE listings SET status='active' WHERE id=?", (listing_id,))
        conn.commit()

    # Post to channel
    channel_msg_id = None
    if item_dict:
        try:
            listing_in = ListingIn(**item_dict)
            user_dict = {
                "id": int(user_id), "first_name": owner_info["first_name"] or "Покупатель",
                "username": owner_info["username"] or "",
            }
            channel_msg_id = await post_to_channel(listing_id, listing_in, user_dict)
        except Exception as e:
            print(f"tinkoff_notify post_to_channel error: {e}", flush=True)

    # Notify user
    try:
        if bot is not None and user_id:
            tier_label = TIER_LABELS.get(tier, tier)
            text = (
                f"✅ <b>Оплата подтверждена!</b>\n\n"
                f"Объявление <code>{listing_id}</code> активировано как <b>{tier_label}</b>.\n\n"
                f"💳 Способ: Тинькофф (ожидаемая сумма {expected_amount_rub} ₽)\n\n"
                f"Оно появилось в ленте и канале @ibaraholkatyt."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Открыть барахолку", web_app=WebAppInfo(url=WEBAPP_URL))]
            ])
            await bot.send_message(user_id, text, reply_markup=kb)
    except Exception as e:
        print(f"payments_activate user notify error: {e}", flush=True)

    log_msg = f"[ACTIVATE] listing={listing_id} user={user_id} channel_msg={channel_msg_id}"
    print(log_msg, flush=True)
    try:
        with open("/data/last_post.log", "a") as f:
            f.write(log_msg + "\n")
    except Exception:
        pass

    return {
        "ok": True,
        "activated": listing_id,
        "channel_message_id": channel_msg_id,
    }


@app.post("/payments/yukassa/webhook")
async def yukassa_webhook(request: Request):
    """YooKassa payment notification. Activates listing when succeeded.
    Configure in YooKassa dashboard: https://yookassa.ru/my/shop/fnsi/notifications
    URL: https://web-production-338982.up.railway.app/payments/yukassa/webhook
    Events: payment.succeeded, payment.canceled
    """
    try:
        body = await request.json()
        event = body.get("event", "")
        obj = body.get("object", {})
        metadata = obj.get("metadata", {})
        listing_id = metadata.get("listing_id", "")
        tier = metadata.get("tier", "")
        user_id = metadata.get("user_id", "")

        log_msg = f"[YUKASSA] event={event} listing={listing_id} status={obj.get('status')}"
        print(log_msg, flush=True)
        try:
            with open("/data/last_post.log", "a") as f:
                f.write(log_msg + "\n")
        except Exception:
            pass

        if event != "payment.succeeded":
            return {"ok": True, "ignored": event}

        if not listing_id:
            return {"ok": False, "error": "no listing_id in metadata"}

        # Mark as paid (NOT active — user must confirm via Mini App button)
        item_dict = None
        with db_cursor() as conn:
            row = conn.execute(
                "SELECT * FROM listings WHERE id=?", (listing_id,)
            ).fetchone()
            if row and row["status"] in ("awaiting_payment", "paid"):
                if row["status"] != "paid":
                    conn.execute(
                        "UPDATE listings SET status='paid', paid_at=extract(epoch from now())::bigint WHERE id=?",
                        (listing_id,),
                    )
                    conn.commit()
                item_dict = {
                    "id": row["id"], "title": row["title"], "description": row["description"],
                    "price": row["price"], "cat": row["cat"], "type": row["type"],
                    "contact": row["contact"], "photo": row["photo"], "tier": row["tier"],
                    "city": row["city"],
                }

        # Notify user via Telegram (ask to open Mini App and press Activate)
        try:
            if user_id and bot is not None:
                amount = obj.get("amount", {}).get("value", "?")
                text = (
                    f"✅ <b>Оплата получена!</b>\n\n"
                    f"Объявление <code>{listing_id}</code> ({tier.upper()}) готово к публикации.\n"
                    f"💰 Списано: {amount} ₽\n\n"
                    f"Откройте Mini App и нажмите «Активировать объявление» — оно появится в канале."
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📱 Открыть барахолку", web_app=WebAppInfo(url=WEBAPP_URL))]
                ])
                await bot.send_message(int(user_id), text, reply_markup=kb)
        except Exception as e:
            print(f"YooKassa webhook notify error: {e}", flush=True)

        return {"ok": True, "activated": listing_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================
# TON Connect payments
# ============================================================

async def _ton_check_tx(to_address: str, amount_nano: int, comment: str, since_ts: int = 0):
    """Check TON Center API for incoming tx matching address + amount + comment.
    Returns (found: bool, tx_hash: str|None).
    """
    try:
        # Get transactions on the wallet (limit 20 most recent).
        url = f"{TONCENTER_API}/getTransactions"
        params = {
            "address": to_address,
            "limit": 20,
            "api_key": os.getenv("TONCENTER_API_KEY", ""),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            return False, None
        data = r.json()
        if not data.get("ok"):
            return False, None
        for tx in data.get("result", []):
            in_msg = tx.get("in_msg") or {}
            value = int(in_msg.get("value", 0) or 0)
            tx_comment = in_msg.get("message", "") or ""
            tx_time = int(tx.get("utime", 0) or 0)
            if value >= amount_nano and tx_comment.strip() == comment.strip() and tx_time >= since_ts:
                return True, tx.get("transaction_id", {}).get("hash", "")
    except Exception as e:
        logger.error(f"_ton_check_tx error: {e}")
    return False, None


@app.post("/payments/ton/create")
async def ton_create_payment(request: Request):
    """Create a TON payment intent for a listing.
    Returns: wallet address, amount (TON), unique comment (used as payment reference).
    """
    try:
        body = await request.json()
        listing_id = body.get("listing_id", "").strip()
        tier = body.get("tier", "").strip()
        if not listing_id or tier not in TON_PRICES:
            return {"ok": False, "error": "invalid listing_id or tier"}

        amount_ton = TON_PRICES[tier]
        amount_nano = int(amount_ton * TON_NANOTON)

        # Unique comment = listing_id + nonce (so tx is uniquely identifiable).
        nonce = _secrets.token_hex(4)
        comment = f"ib_{listing_id[:12]}_{nonce}"

        # Persist pending payment row so verify can match.
        now = int(time.time())
        with db_cursor() as conn:
            # Create payments table on first run.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ton_payments (
                    id BIGSERIAL PRIMARY KEY,
                    listing_id TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    amount_nano BIGINT NOT NULL,
                    comment TEXT NOT NULL UNIQUE,
                    tx_hash TEXT,
                    user_id BIGINT,
                    created INTEGER NOT NULL,
                    confirmed INTEGER,
                    tx_time INTEGER
                )
            """)
            conn.execute(
                "INSERT INTO ton_payments (listing_id, tier, amount_nano, comment, created) "
                "VALUES (?, ?, ?, ?, ?)",
                (listing_id, tier, amount_nano, comment, now),
            )
            conn.commit()

        wallet = TON_WALLET_ADDRESS
        if wallet.startswith("UQPLACEHOLDER"):
            return {
                "ok": False,
                "error": "TON_WALLET_ADDRESS not configured (set in Render env)",
            }

        return {
            "ok": True,
            "wallet": wallet,
            "amount_ton": amount_ton,
            "amount_nano": amount_nano,
            "comment": comment,
            "listing_id": listing_id,
            "tier": tier,
            "instructions": (
                f"Send exactly {amount_ton} TON to {wallet} "
                f"with comment '{comment}'. "
                "Tap 'Verify' after sending."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/payments/ton/verify")
async def ton_verify_payment(request: Request):
    """Verify a TON payment by checking on-chain for the comment + amount.
    Activates listing tier on success.
    """
    try:
        body = await request.json()
        listing_id = body.get("listing_id", "").strip()
        comment = body.get("comment", "").strip()
        user_id = body.get("user_id", 0)
        if not listing_id or not comment:
            return {"ok": False, "error": "listing_id and comment required"}

        # Look up pending payment.
        with db_cursor() as conn:
            row = conn.execute(
                "SELECT * FROM ton_payments WHERE comment=? AND listing_id=?",
                (comment, listing_id),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "payment intent not found"}
            if row.get("confirmed"):
                # Already confirmed; idempotent.
                # If deal, return escrowed state
                if row["tier"] == "deal":
                    return {"ok": True, "already_confirmed": True, "deal_id": listing_id, "status": "escrowed"}
                return {"ok": True, "already_confirmed": True, "listing_id": listing_id}
            tier = row["tier"]
            amount_nano = int(row["amount_nano"])
            created_ts = int(row["created"])

        # Check on-chain.
        found, tx_hash = await _ton_check_tx(
            TON_WALLET_ADDRESS, amount_nano, comment, since_ts=created_ts - 60
        )
        if not found:
            return {
                "ok": False,
                "verified": False,
                "error": "tx not found yet — wait 30s and tap Verify again",
            }

        # Mark confirmed.
        now = int(time.time())
        with db_cursor() as conn:
            conn.execute(
                "UPDATE ton_payments SET confirmed=?, tx_hash=?, tx_time=?, user_id=? "
                "WHERE comment=?",
                (now, tx_hash, now, user_id, comment),
            )

            # ESCROW DEAL branch: tier='deal', listing_id is the deal_id
            if tier == "deal":
                deal_row = conn.execute(
                    "SELECT * FROM deals WHERE id=? AND buyer_id=?",
                    (listing_id, user_id),
                ).fetchone()
                if not deal_row:
                    return {"ok": False, "error": "deal_not_found"}
                if _deal_row_to_dict(deal_row)["status"] == "escrowed":
                    return {"ok": True, "verified": True, "deal_id": listing_id, "status": "escrowed",
                            "tx_hash": tx_hash, "already_confirmed": True}
                conn.execute(
                    "UPDATE deals SET status='escrowed', paid_at=?, escrow_tx_hash=? WHERE id=?",
                    (now, tx_hash, listing_id),
                )
                conn.commit()
                logging.info(f"DEAL_ESCROWED deal={listing_id} user={user_id} via=TON tx={tx_hash[:16]}")
                return {"ok": True, "verified": True, "deal_id": listing_id, "status": "escrowed", "tx_hash": tx_hash}

            # LISTING branch (regular paid listing)
            row = conn.execute(
                "SELECT * FROM listings WHERE id=?", (listing_id,)
            ).fetchone()
            if row and row["status"] in ("awaiting_payment", "paid"):
                if row["status"] != "paid":
                    conn.execute(
                        "UPDATE listings SET status='paid', paid_at=extract(epoch from now())::bigint WHERE id=?",
                        (listing_id,),
                    )
                    conn.commit()
                item_dict = {
                    "id": row["id"], "title": row["title"], "description": row["description"],
                    "price": row["price"], "cat": row["cat"], "type": row["type"],
                    "contact": row["contact"], "photo": row["photo"], "tier": row["tier"],
                    "city": row["city"],
                }
            else:
                item_dict = None

        # Notify user (Mini App polling will pick up status='paid' and show Activate button)
        try:
            if user_id and bot is not None:
                if tier == "deal":
                    text = (
                        f"🛡 <b>Оплата TON получена!</b>\n\n"
                        f"Сделка <code>{listing_id}</code> переведена в статус «в гаранте».\n"
                        f"💎 Списано: {amount_nano / TON_NANOTON} TON\n"
                        f"🔗 Tx: <code>{tx_hash[:16]}…</code>\n\n"
                        f"Продавец скоро отправит товар. Следите за статусом в Mini App → «💼 Сделки»."
                    )
                else:
                    text = (
                        f"✅ <b>Оплата TON получена!</b>\n\n"
                        f"Объявление <code>{listing_id}</code> ({tier.upper()}) готово к публикации.\n"
                        f"💎 Списано: {TON_PRICES[tier]} TON\n"
                        f"🔗 Tx: <code>{tx_hash[:16]}…</code>\n\n"
                        f"Откройте Mini App и нажмите «Активировать объявление»."
                    )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📱 Открыть барахолку", web_app=WebAppInfo(url=WEBAPP_URL))]
                ])
                await bot.send_message(int(user_id), text, reply_markup=kb)
        except Exception as e:
            print(f"TON verify notify error: {e}", flush=True)

        if tier == "deal":
            return {
                "ok": True,
                "verified": True,
                "deal_id": listing_id,
                "status": "escrowed",
                "tx_hash": tx_hash,
            }
        return {
            "ok": True,
            "verified": True,
            "listing_id": listing_id,
            "tier": tier,
            "tx_hash": tx_hash,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/payments/ton/wallet")
async def ton_wallet_info():
    """Public info: wallet address + tier prices for the frontend."""
    return {
        "ok": True,
        "wallet": TON_WALLET_ADDRESS,
        "configured": not TON_WALLET_ADDRESS.startswith("UQPLACEHOLDER"),
        "prices": TON_PRICES,
    }


# ============================================================
# ADS / IB COINS — смотри рекламу, получай внутреннюю валюту
# ============================================================
# 1 IB Coin = 1 Telegram Star. Юзер смотрит рекламу → получает IB Coins → тратит их на оплату объявлений.
# Реальные Telegram Stars нельзя выдавать бесплатно (нарушение ToS), поэтому это внутренняя валюта,
# которую мы обмениваем на свои услуги (оплата listing'ов). Anti-fraud: 1 просмотр на юзера в 30 сек.

AD_COOLDOWN_SEC = 30  # минимум секунд между просмотрами
AD_DEFAULT_REWARD = 10  # IB Coins за просмотр по умолчанию

# Демо-рекламные креативы — заполняются при первом старте если таблица пустая
SEED_AD_CREATIVES = [
    {
        "title": "Apple AirPods Pro 2",
        "description": "Новые. Гарантия 1 год. Доставка по Москве сегодня.",
        "image_url": "",
        "click_url": "https://t.me/Ibaraholka_bot",
        "reward_coins": 10,
        "duration_sec": 8,
    },
    {
        "title": "Ремонт iPhone в Москве",
        "description": "Замена экрана от 30 мин. Гарантия 90 дней. Рядом с метро.",
        "image_url": "",
        "click_url": "https://t.me/Ibaraholka_bot",
        "reward_coins": 10,
        "duration_sec": 8,
    },
    {
        "title": "Trade-in iPhone",
        "description": "Сдай старый — получи скидку на новый. Оценка за 5 минут.",
        "image_url": "",
        "click_url": "https://t.me/Ibaraholka_bot",
        "reward_coins": 10,
        "duration_sec": 8,
    },
    {
        "title": "iPhone 15 Pro Max",
        "description": "В наличии. Все цвета. Trade-in с доплатой.",
        "image_url": "",
        "click_url": "https://t.me/Ibaraholka_bot",
        "reward_coins": 10,
        "duration_sec": 8,
    },
]


# Lazy seed: called inside endpoint, after init_db()
def _seed_ads_if_empty():
    """Insert seed ads on first start (idempotent)."""
    now = int(datetime.now().timestamp())  # seconds, fits in PG INTEGER
    with db_cursor() as conn:
        cur = conn.execute("SELECT COUNT(*) AS c FROM ad_creatives")
        row = cur.fetchone()
        # Support both psycopg2 dict-style and sqlite3 tuple-style
        count = row["c"] if isinstance(row, dict) and "c" in row else (row["c"] if isinstance(row, dict) else row[0])
        if count == 0:
            for i, ad in enumerate(SEED_AD_CREATIVES, start=1):
                conn.execute(
                    "INSERT INTO ad_creatives (id, title, description, image_url, click_url, reward_coins, duration_sec, enabled, weight, created) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
                    (i, ad["title"], ad["description"], ad["image_url"], ad["click_url"],
                     ad["reward_coins"], ad["duration_sec"], now),
                )
            conn.commit()


def _credit_due_ads(conn, user_id: int, now_sec: int) -> int:
    """Auto-credit all 'due' ad views for user (created + duration_sec <= now_sec).

    Returns total coins credited this call. Used by /user/balance, /ads/next,
    /payments/coins/pay — guarantees coins arrive even if user closed the app.

    Works with both psycopg2 RealDictCursor and sqlite3 tuple-style rows.
    """
    def _g(row, key, idx):
        if isinstance(row, dict):
            return row.get(key)
        return row[idx]
    try:
        rows = conn.execute(
            "SELECT v.id, v.ad_id, v.coins_credited "
            "FROM ad_views v WHERE v.user_id=? AND v.completed=0 "
            "AND EXISTS (SELECT 1 FROM ad_creatives a WHERE a.id=v.ad_id AND v.created + a.duration_sec <= ?)",
            (user_id, now_sec),
        ).fetchall()
    except Exception as e:
        logging.warning("_credit_due_ads query failed: %s", e)
        return 0
    if not rows:
        return 0
    total = 0
    credited_ad_ids = []
    for r in rows:
        view_id = _g(r, "id", 0)
        ad_id = _g(r, "ad_id", 1)
        coins = _g(r, "coins_credited", 2) or 0
        total += int(coins)
        credited_ad_ids.append(int(ad_id))
        conn.execute(
            "UPDATE ad_views SET completed=1 WHERE id=?",
            (view_id,),
        )
    if total > 0:
        bal = conn.execute(
            "SELECT coins, total_earned FROM user_balances WHERE user_id=?",
            (user_id,),
        ).fetchone()
        new_coins, new_earned = 0, 0
        if bal:
            cur_coins = _g(bal, "coins", 0) or 0
            cur_earned = _g(bal, "total_earned", 1) or 0
            new_coins = cur_coins + total
            new_earned = cur_earned + total
            conn.execute(
                "UPDATE user_balances SET coins=?, total_earned=?, updated=? WHERE user_id=?",
                (new_coins, new_earned, now_sec, user_id),
            )
        else:
            new_coins = total
            new_earned = total
            conn.execute(
                "INSERT INTO user_balances (user_id, coins, total_earned, total_spent, updated) "
                "VALUES (?, ?, ?, 0, ?)",
                (user_id, new_coins, new_earned, now_sec),
            )
    return total


@app.post("/ads/start")
async def ads_start(request: Request, user: Dict = Depends(get_user)):
    """User started watching an ad. Records a PENDING view (completed=0).

    Server auto-credits when duration_sec passes — client doesn't have to
    stay on the page. Just check /user/balance later.
    """
    user_id = int(user["id"])
    now_sec = int(datetime.now().timestamp())
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    ad_id = int(body.get("ad_id", 0))
    if not ad_id:
        raise HTTPException(400, "ad_id required")

    try:
        _seed_ads_if_empty()
    except Exception as e:
        logging.warning("seed_ads in /ads/start failed: %s", e)

    with db_cursor() as conn:
        # Cancel any prior pending views (user clicked again on a new ad)
        conn.execute(
            "UPDATE ad_views SET completed=1, coins_credited=0 WHERE user_id=? AND completed=0",
            (user_id,),
        )
        ad_row = conn.execute(
            "SELECT reward_coins, duration_sec, enabled FROM ad_creatives WHERE id=?",
            (ad_id,),
        ).fetchone()
        if not ad_row:
            return {"ok": False, "error": "ad_not_found"}
        # dict/tuple agnostic
        if isinstance(ad_row, dict):
            enabled = ad_row.get("enabled")
            reward = ad_row.get("reward_coins") or 0
            duration_sec = ad_row.get("duration_sec") or 0
        else:
            enabled = ad_row[2]
            reward = ad_row[0] or 0
            duration_sec = ad_row[1] or 0
        if not enabled:
            return {"ok": False, "error": "ad_disabled"}
        view_id = (now_sec * 1000) ^ user_id
        conn.execute(
            "INSERT INTO ad_views (id, user_id, ad_id, coins_credited, created, completed) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (view_id, user_id, ad_id, reward, now_sec),
        )
        conn.commit()
    return {
        "ok": True,
        "ad_id": ad_id,
        "duration_sec": int(duration_sec),
        "reward": int(reward),
        "pending_until_ts": now_sec + int(duration_sec),
        "message": f"+{reward} ⭐ начислится через {duration_sec} сек автоматически",
    }


@app.get("/ads/next")
async def ads_next(user: Dict = Depends(get_user)):
    """Return the next ad creative for this user. Anti-fraud: refuses if last view was < 30s ago."""
    try:
        _seed_ads_if_empty()
    except Exception as e:
        logging.warning("seed_ads in /ads/next failed: %s", e)
    user_id = int(user["id"])
    now_sec = int(datetime.now().timestamp())

    def _g(row, key, idx):
        return row.get(key) if isinstance(row, dict) else row[idx]

    with db_cursor() as conn:
        # Auto-credit any ads whose duration has already passed (server-side timer)
        credited_now = _credit_due_ads(conn, user_id, now_sec)
        if credited_now > 0:
            conn.commit()
        # Anti-fraud: последний просмотр
        last = conn.execute(
            "SELECT created FROM ad_views WHERE user_id=? ORDER BY created DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if last and (now_sec - _g(last, "created", 0)) < AD_COOLDOWN_SEC:
            wait_sec = AD_COOLDOWN_SEC - int((now_sec - _g(last, "created", 0)))
            return {
                "ok": False,
                "reason": "cooldown",
                "wait_sec": max(wait_sec, 1),
                "message": f"Подождите {wait_sec} сек до следующей рекламы",
            }

        # Берём случайное активное объявление (weighted by weight, без повтора последнего)
        last_ad_row = conn.execute(
            "SELECT ad_id FROM ad_views WHERE user_id=? ORDER BY created DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        last_ad_id = _g(last_ad_row, "ad_id", 0) if last_ad_row else None

        ads = conn.execute(
            "SELECT id, title, description, image_url, click_url, reward_coins, duration_sec "
            "FROM ad_creatives WHERE enabled=1 ORDER BY weight DESC, RANDOM() LIMIT 20"
        ).fetchall()
        if not ads:
            return {"ok": False, "reason": "no_ads", "message": "Нет активной рекламы"}

        # Prefer ads different from last shown
        candidates = [a for a in ads if _g(a, "id", 0) != last_ad_id] or ads
        ad = candidates[0]
        return {
            "ok": True,
            "ad": {
                "id": _g(ad, "id", 0),
                "title": _g(ad, "title", 1),
                "description": _g(ad, "description", 2),
                "image_url": _g(ad, "image_url", 3),
                "click_url": _g(ad, "click_url", 4),
                "reward_coins": _g(ad, "reward_coins", 5),
                "duration_sec": _g(ad, "duration_sec", 6),
            },
        }


@app.post("/ads/watch-complete")
async def ads_watch_complete(request: Request, user: Dict = Depends(get_user)):
    """User finished watching ad (after duration_sec). Credit IB Coins.

    Legacy endpoint — kept for backward compat. New flow uses /ads/start +
    auto-credit. Server re-checks: cooldown, ad exists & enabled.
    """
    user_id = int(user["id"])
    now_sec = int(datetime.now().timestamp())

    def _g(row, key, idx):
        return row.get(key) if isinstance(row, dict) else row[idx]

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")

    ad_id = int(body.get("ad_id", 0))
    if not ad_id:
        raise HTTPException(400, "ad_id required")

    with db_cursor() as conn:
        # Re-fetch ad for reward value
        ad_row = conn.execute(
            "SELECT reward_coins, duration_sec, enabled FROM ad_creatives WHERE id=?",
            (ad_id,),
        ).fetchone()
        if not ad_row or not _g(ad_row, "enabled", 2):
            return {"ok": False, "error": "ad_disabled"}
        reward = _g(ad_row, "reward_coins", 0) or 0

        # Anti-fraud: cooldown check
        last = conn.execute(
            "SELECT created FROM ad_views WHERE user_id=? ORDER BY created DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if last and (now_sec - _g(last, "created", 0)) < AD_COOLDOWN_SEC:
            wait_sec = AD_COOLDOWN_SEC - int(now_sec - _g(last, "created", 0))
            return {"ok": False, "error": "cooldown", "wait_sec": max(wait_sec, 1)}

        # Insert view record + update balance
        view_id = (now_sec * 1000) ^ user_id
        conn.execute(
            "INSERT INTO ad_views (id, user_id, ad_id, coins_credited, created, completed) VALUES (?, ?, ?, ?, ?, 1)",
            (view_id, user_id, ad_id, reward, now_sec),
        )
        conn.execute(
            "UPDATE ad_creatives SET shown_count = shown_count + 1 WHERE id=?",
            (ad_id,),
        )

        # Upsert balance
        bal = conn.execute(
            "SELECT coins, total_earned FROM user_balances WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if bal:
            new_coins = (_g(bal, "coins", 0) or 0) + reward
            new_earned = (_g(bal, "total_earned", 1) or 0) + reward
            conn.execute(
                "UPDATE user_balances SET coins=?, total_earned=?, updated=? WHERE user_id=?",
                (new_coins, new_earned, now_sec, user_id),
            )
        else:
            new_coins = reward
            new_earned = reward
            conn.execute(
                "INSERT INTO user_balances (user_id, coins, total_earned, total_spent, updated) VALUES (?, ?, ?, 0, ?)",
                (user_id, new_coins, new_earned, now_sec),
            )
        conn.commit()

        return {
            "ok": True,
            "credited": reward,
            "balance": new_coins,
            "total_earned": new_earned,
            "next_available_sec": AD_COOLDOWN_SEC,
        }


@app.post("/ads/click")
async def ads_click(request: Request, user: Dict = Depends(get_user)):
    """Track that user clicked the ad (analytics only, no reward)."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    ad_id = int(body.get("ad_id", 0))
    if not ad_id:
        return {"ok": False, "error": "ad_id required"}
    with db_cursor() as conn:
        conn.execute("UPDATE ad_creatives SET click_count = click_count + 1 WHERE id=?", (ad_id,))
        conn.commit()
    return {"ok": True}


@app.get("/user/balance")
async def user_balance(user: Dict = Depends(get_user)):
    """Return current IB Coins balance + lifetime totals."""
    user_id = int(user["id"])
    now_sec = int(datetime.now().timestamp())
    with db_cursor() as conn:
        # Auto-credit any ads whose duration has already passed
        credited_now = _credit_due_ads(conn, user_id, now_sec)
        if credited_now > 0:
            conn.commit()
        bal = conn.execute(
            "SELECT coins, total_earned, total_spent, updated FROM user_balances WHERE user_id=?",
            (user_id,),
        ).fetchone()
    def _g(row, key, idx):
        return row.get(key) if isinstance(row, dict) else row[idx]
    if bal:
        return {
            "ok": True,
            "coins": _g(bal, "coins", 0) or 0,
            "total_earned": _g(bal, "total_earned", 1) or 0,
            "total_spent": _g(bal, "total_spent", 2) or 0,
            "updated": _g(bal, "updated", 3) or 0,
            "credited_now": credited_now,
        }
    return {"ok": True, "coins": 0, "total_earned": 0, "total_spent": 0, "updated": 0, "credited_now": credited_now}


@app.post("/payments/coins/pay")
async def payments_coins_pay(request: Request, user: Dict = Depends(get_user)):
    """Pay for a listing with IB Coins.

    Body: {listing_id}
    Rules:
    - listing must belong to user
    - listing.status must be 'awaiting_payment' or 'paid'
    - price_coins = TIER_PRICES[tier] (50 for premium, 150 for vip, 0 for free)
    - coins >= price_coins → deduct, activate + post to channel
    - else → 402 "insufficient funds"
    """
    user_id = int(user["id"])
    now_sec = int(datetime.now().timestamp())
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    listing_id = body.get("listing_id", "")
    if not listing_id:
        raise HTTPException(400, "listing_id required")

    def _g(row, key, idx):
        return row.get(key) if isinstance(row, dict) else row[idx]

    with db_cursor() as conn:
        # Auto-credit any ads whose duration has already passed
        credited_now = _credit_due_ads(conn, user_id, now_sec)
        if credited_now > 0:
            conn.commit()
        # Lock listing row
        row = conn.execute(
            "SELECT id, user_id, tier, status, title, price FROM listings WHERE id=?",
            (listing_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "listing_not_found"}
        if int(_g(row, "user_id", 1)) != user_id:
            return {"ok": False, "error": "not_owner"}
        tier = _g(row, "tier", 2)
        status = _g(row, "status", 3)
        title = _g(row, "title", 4)

        if tier == "free":
            return {"ok": False, "error": "free_no_payment"}

        price_coins = TIER_PRICES.get(tier, 0)
        if price_coins <= 0:
            return {"ok": False, "error": "invalid_tier"}

        if status not in ("awaiting_payment", "paid"):
            return {"ok": False, "error": "bad_status", "current_status": status}

        # Balance check
        bal = conn.execute(
            "SELECT coins FROM user_balances WHERE user_id=?",
            (user_id,),
        ).fetchone()
        coins = _g(bal, "coins", 0) if bal else 0
        coins = coins or 0
        if coins < price_coins:
            need = price_coins - coins
            return {
                "ok": False,
                "error": "insufficient_funds",
                "have": coins,
                "need": price_coins,
                "missing": need,
                "message": f"Не хватает {need} IB Coins. Посмотрите ещё рекламу.",
            }

        # Deduct + activate
        new_coins = coins - price_coins
        conn.execute(
            "UPDATE user_balances SET coins=?, total_spent=total_spent+?, updated=? WHERE user_id=?",
            (new_coins, price_coins, now_sec, user_id),
        )

        # Activate listing (set expires_at if missing)
        expires_at = int(datetime.now().timestamp()) + TIER_DURATIONS.get(tier, 7 * 86400)
        conn.execute(
            "UPDATE listings SET status='active', paid_at=?, expires_at=? WHERE id=?",
            (now_sec, expires_at, listing_id),
        )
        conn.commit()

    # Post to channel (outside the DB transaction so we can use bot)
    post_result = None
    try:
        if bot:
            post_result = await post_to_channel(listing_id)
    except Exception as e:
        logging.warning("post_to_channel after coins payment failed: %s", e)

    # Notify user
    try:
        if bot:
            await bot.send_message(
                user_id,
                f"🪙 Оплачено {price_coins} IB Coins!\n\n"
                f"📦 <b>{title}</b>\n\n"
                f"✅ Объявление активировано и опубликовано в канале.",
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        pass

    return {
        "ok": True,
        "charged": price_coins,
        "balance": new_coins,
        "listing_id": listing_id,
        "status": "active",
        "posted": post_result is not None,
    }


# ============================================================
# ESCROW / DEALS — Гарант сделки
# ============================================================
# Сделка: покупатель платит → деньги в эскроу → продавец отправляет → покупатель подтверждает → release.
# Статусы: awaiting_payment → escrowed → shipped → released
#                                   ↘ disputed → (admin resolve) → released / refunded
#                                   ↘ cancelled (до оплаты)
#                                   ↘ refunded (если продавец не отправил за 3 дня)
# Автоподтверждение: shipped + 5 дней без подтверждения покупателем → release автоматом.
DEAL_AUTO_REFUND_DAYS = 3      # после escrowed, если продавец не ship → refund
DEAL_AUTO_RELEASE_DAYS = 5     # после shipped, если покупатель не confirm → release


def _deal_row_to_dict(row) -> Dict[str, Any]:
    """Convert deals row → JSON-safe dict."""
    def g(k, i):
        return row.get(k) if isinstance(row, dict) else row[i]
    return {
        "id": g("id", 0),
        "listing_id": g("listing_id", 1),
        "buyer_id": int(g("buyer_id", 2) or 0),
        "buyer_name": g("buyer_name", 3),
        "buyer_username": g("buyer_username", 4),
        "seller_id": int(g("seller_id", 5) or 0),
        "seller_name": g("seller_name", 6),
        "seller_username": g("seller_username", 7),
        "amount_rub": int(g("amount_rub", 8) or 0),
        "amount_nano": int(g("amount_nano", 9) or 0) if g("amount_nano", 9) else None,
        "currency": g("currency", 10),
        "payment_method": g("payment_method", 11),
        "status": g("status", 12),
        "shipping_address": g("shipping_address", 13),
        "shipping_city": g("shipping_city", 14),
        "tracking": g("tracking", 15),
        "dispute_reason": g("dispute_reason", 16),
        "dispute_resolution": g("dispute_resolution", 17),
        "escrow_tx_hash": g("escrow_tx_hash", 18),
        "payout_tx_hash": g("payout_tx_hash", 19),
        "created": int(g("created", 20) or 0),
        "paid_at": int(g("paid_at", 21) or 0) if g("paid_at", 21) else None,
        "shipped_at": int(g("shipped_at", 22) or 0) if g("shipped_at", 22) else None,
        "confirmed_at": int(g("confirmed_at", 23) or 0) if g("confirmed_at", 23) else None,
        "closed_at": int(g("closed_at", 24) or 0) if g("closed_at", 24) else None,
        "auto_release_at": int(g("auto_release_at", 25) or 0) if g("auto_release_at", 25) else None,
    }


def _deal_notify(bot, deal_row, event: str):
    """Send Telegram notification to both buyer and seller about deal event."""
    if not bot or not deal_row:
        return
    buyer_id = deal_row.get("buyer_id")
    seller_id = deal_row.get("seller_id")
    deal_id = deal_row.get("id")
    title_text = {
        "created": f"🛡 Сделка #{deal_id} создана. Оплатите в течение 24 часов.",
        "paid": f"💰 Сделка #{deal_id} оплачена! Деньги в гаранте. Продавец скоро отправит товар.",
        "shipped": f"📦 Продавец отправил ваш заказ по сделке #{deal_id}. Трек: {deal_row.get('tracking') or 'не указан'}",
        "released": f"✅ Сделка #{deal_id} закрыта успешно! Спасибо за использование гаранта.",
        "refunded": f"↩️ Сделка #{deal_id} отменена. Деньги возвращены покупателю.",
        "disputed": f"⚠️ Открыт спор по сделке #{deal_id}. Админ свяжется с вами.",
    }.get(event)
    if not title_text:
        return
    kb = None
    if event in ("paid", "shipped", "released"):
        kb_url = f"{WEBAPP_URL}?startapp=deal_{deal_id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Открыть сделку", url=kb_url)],
        ])
    for uid in (buyer_id, seller_id):
        if not uid:
            continue
        try:
            asyncio.create_task(bot.send_message(int(uid), title_text, reply_markup=kb))
        except Exception as e:
            logging.warning(f"deal_notify failed for {uid}: {e}")


async def _ton_transfer(to_address: str, amount_nano: int, comment: str = "") -> Optional[str]:
    """Send TON from our escrow wallet to destination. Returns tx_hash or None.

    NOTE: requires bot's wallet private key (TON_ESCROW_MNEMONIC env). For MVP this is a
    placeholder — actual on-chain transfer would be done by admin bot command / bot wallet.
    For now we mark the transfer as 'manual' and rely on admin to process payouts.
    """
    # Real implementation requires TON wallet SDK (ton-core / tonsdk) + signing key.
    # For MVP we don't have private key in env — flag for manual admin payout.
    logging.warning(
        f"_ton_transfer requested: to={to_address} amount_nano={amount_nano} comment={comment!r} — manual mode"
    )
    return None  # tx_hash will be set later by admin via /admin/payouts/{id}/complete


def _seller_balance_credit(conn, user_id: int, currency: str, amount: int) -> int:
    """Add to seller balance (RUB=kopeyki, TON=nano). Returns new balance."""
    now = int(time.time())
    row = conn.execute(
        "SELECT amount FROM seller_balances WHERE user_id=? AND currency=?",
        (user_id, currency),
    ).fetchone()
    cur = int(row["amount"] if isinstance(row, dict) else row[0]) if row else 0
    new_amount = cur + amount
    if row:
        conn.execute(
            "UPDATE seller_balances SET amount=?, updated=? WHERE user_id=? AND currency=?",
            (new_amount, now, user_id, currency),
        )
    else:
        conn.execute(
            "INSERT INTO seller_balances (user_id, currency, amount, updated) VALUES (?, ?, ?, ?)",
            (user_id, currency, new_amount, now),
        )
    return new_amount


def _deal_settle_release(conn, deal_row) -> Dict[str, Any]:
    """Move deal to 'released' status: credit seller's balance + record payout.

    For TON currency: trigger outgoing TON transfer (async).
    For RUB currency: credit seller_balances (RUB) — admin pays out via /admin/payouts.
    """
    def g(k, i):
        return deal_row.get(k) if isinstance(deal_row, dict) else deal_row[i]
    deal_id = g("id", 0)
    seller_id = int(g("seller_id", 5) or 0)
    amount_rub = int(g("amount_rub", 8) or 0)
    amount_nano = int(g("amount_nano", 9) or 0) if g("amount_nano", 9) else 0
    currency = g("currency", 10)

    now = int(time.time())
    payout_tx = None
    if currency == "TON" and amount_nano > 0:
        # Mark as manual payout (no private key in env yet)
        payout_tx = "manual_pending"
        # Credit seller balance for tracking
        _seller_balance_credit(conn, seller_id, "TON", amount_nano)
    else:
        # RUB: credit seller balance
        _seller_balance_credit(conn, seller_id, "RUB", amount_rub * 100)  # store in kopeyki

    conn.execute(
        "UPDATE deals SET status='released', confirmed_at=?, closed_at=?, payout_tx_hash=? WHERE id=?",
        (now, now, payout_tx, deal_id),
    )
    conn.commit()
    return {"status": "released", "payout_tx_hash": payout_tx}


@app.post("/deals/create")
async def deals_create(request: Request, user: Dict = Depends(get_user)):
    """Buyer initiates a deal: creates awaiting_payment row + returns payment details.

    Body: {listing_id, payment_method: 'tinkoff'|'yukassa'|'ton', shipping_address, shipping_city}
    Returns: deal info + payment instructions (link/wallet/comment).
    """
    try:
        body = await request.json()
        listing_id = (body.get("listing_id") or "").strip()
        payment_method = (body.get("payment_method") or "").strip().lower()
        shipping_address = (body.get("shipping_address") or "").strip()
        shipping_city = (body.get("shipping_city") or "").strip()
        if not listing_id:
            return {"ok": False, "error": "listing_id required"}
        if payment_method not in ("tinkoff", "yukassa", "ton"):
            return {"ok": False, "error": "payment_method must be tinkoff|yukassa|ton"}
        if not shipping_address:
            return {"ok": False, "error": "shipping_address required"}
    except Exception as e:
        return {"ok": False, "error": f"bad_request: {e}"}

    buyer_id = int(user["id"])
    buyer_name = user.get("first_name") or "Покупатель"
    buyer_username = user.get("username")

    now = int(time.time())
    deal_id = "D-" + _secrets.token_hex(4).upper()
    out: Dict[str, Any] = {"ok": False}

    with db_cursor() as conn:
        row = conn.execute(
            "SELECT id, user_id, user_name, user_username, title, price, status "
            "FROM listings WHERE id=?",
            (listing_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "listing_not_found"}
        title = row["title"] if isinstance(row, dict) else row[4]
        seller_id = int(row["user_id"] if isinstance(row, dict) else row[1])
        if seller_id == buyer_id:
            return {"ok": False, "error": "cannot_deal_with_self"}
        seller_name = (row["user_name"] if isinstance(row, dict) else row[2]) or "Продавец"
        seller_username = (row["user_username"] if isinstance(row, dict) else row[3])
        price_rub = int(row["price"] if isinstance(row, dict) else row[5])

        # Calculate amount in TON (~rate: 1 TON ≈ 280 RUB for MVP, configurable later)
        ton_rate_rub = 280
        amount_nano = int(price_rub / ton_rate_rub * TON_NANOTON) if payment_method == "ton" else None
        currency = "TON" if payment_method == "ton" else "RUB"

        conn.execute(
            "INSERT INTO deals (id, listing_id, buyer_id, buyer_name, buyer_username, "
            "seller_id, seller_name, seller_username, amount_rub, amount_nano, currency, "
            "payment_method, status, shipping_address, shipping_city, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?, ?, ?)",
            (deal_id, listing_id, buyer_id, buyer_name, buyer_username,
             seller_id, seller_name, seller_username, price_rub, amount_nano, currency,
             payment_method, shipping_address, shipping_city, now),
        )
        conn.commit()

        # Payment instructions
        if payment_method == "ton":
            # Use a unique comment so the on-chain transfer can be matched
            nonce = _secrets.token_hex(4)
            comment = f"deal_{deal_id}_{nonce}"
            # Persist pending link so /payments/ton/verify can find this deal
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ton_payments ("
                "id BIGSERIAL PRIMARY KEY, listing_id TEXT, tier TEXT, "
                "amount_nano BIGINT, comment TEXT NOT NULL UNIQUE, "
                "tx_hash TEXT, user_id BIGINT, created INTEGER, confirmed INTEGER, tx_time INTEGER)"
            )
            conn.execute(
                "INSERT INTO ton_payments (listing_id, tier, amount_nano, comment, created) "
                "VALUES (?, 'deal', ?, ?, ?)",
                (deal_id, amount_nano, comment, now),
            )
            conn.commit()
            wallet = TON_WALLET_ADDRESS
            if wallet.startswith("UQPLACEHOLDER"):
                return {"ok": False, "error": "TON_WALLET_ADDRESS not configured"}
            out = {
                "ok": True,
                "deal_id": deal_id,
                "payment": {
                    "method": "ton",
                    "wallet": wallet,
                    "amount_ton": amount_nano / TON_NANOTON,
                    "amount_nano": amount_nano,
                    "comment": comment,
                    "instructions": (
                        f"Отправьте ровно {amount_nano / TON_NANOTON} TON на {wallet} "
                        f"с комментарием {comment!r}. Деньги попадут в гарант."
                    ),
                },
                "amount_rub": price_rub,
                "currency": currency,
            }
        elif payment_method == "tinkoff":
            # Generate tinkoff quick-pay link with deal_id in label so /payments/tinkoff/notify can route
            pay_url = (
                f"https://www.tbank.ru/rm/r_TGugYbYVEb.mLmrPUwlTy"
                f"?amount={price_rub * 100}&label=deal-{deal_id}"
                f"&successURL=https://t.me/Ibaraholka_bot"
            )
            out = {
                "ok": True,
                "deal_id": deal_id,
                "payment": {
                    "method": "tinkoff",
                    "url": pay_url,
                    "amount_rub": price_rub,
                    "instructions": (
                        f"Оплатите {price_rub} ₽ по ссылке. В комментарии перевода ничего указывать "
                        f"не нужно — мы свяжем платёж со сделкой #{deal_id} автоматически."
                    ),
                },
                "amount_rub": price_rub,
                "currency": currency,
            }
        else:  # yukassa
            # Return YooMoney-style quickpay (fallback; ЮMoney quickpay is dead — use tinkoff link)
            return {
                "ok": False,
                "error": "yukassa_unavailable",
                "message": "Оплата через ЮMoney временно недоступна. Выберите Тинькофф или TON.",
            }

        # Fire-and-forget Telegram notification to seller
        if bot is not None:
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Открыть сделку", url=f"{WEBAPP_URL}?startapp=deal_{deal_id}")],
                ])
                asyncio.create_task(bot.send_message(
                    seller_id,
                    f"🛡 Новая сделка #{deal_id}!\n"
                    f"Покупатель: {buyer_name} (@{buyer_username or '—'})\n"
                    f"Товар: {title}\n"
                    f"Сумма: {price_rub} ₽\n"
                    f"Доставка: {shipping_city}, {shipping_address[:60]}",
                    reply_markup=kb,
                ))
            except Exception as e:
                logging.warning(f"seller notify failed: {e}")
    return out


@app.post("/deals/{deal_id}/ship")
async def deals_ship(deal_id: str, request: Request, user: Dict = Depends(get_user)):
    """Seller marks deal as shipped. Sets tracking, status=shipped, shipped_at=now."""
    user_id = int(user["id"])
    try:
        body = await request.json()
        tracking = (body.get("tracking") or "").strip()
    except Exception:
        tracking = ""
    now = int(time.time())

    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "deal_not_found"}
        d = _deal_row_to_dict(row)
        if d["seller_id"] != user_id:
            return {"ok": False, "error": "not_seller"}
        if d["status"] != "escrowed":
            return {"ok": False, "error": f"invalid_status:{d['status']}"}
        conn.execute(
            "UPDATE deals SET status='shipped', tracking=?, shipped_at=?, "
            "auto_release_at=? WHERE id=?",
            (tracking or None, now, now + DEAL_AUTO_RELEASE_DAYS * 86400, deal_id),
        )
        conn.commit()

    # Notify buyer
    if bot is not None:
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Открыть сделку", url=f"{WEBAPP_URL}?startapp=deal_{deal_id}")],
            ])
            asyncio.create_task(bot.send_message(
                d["buyer_id"],
                f"📦 Продавец отправил ваш заказ по сделке #{deal_id}!\n"
                f"Трек: {tracking or 'не указан'}\n"
                f"Когда получите — откройте сделку и нажмите «Подтвердить получение».",
                reply_markup=kb,
            ))
        except Exception as e:
            logging.warning(f"buyer ship notify failed: {e}")

    return {"ok": True, "deal_id": deal_id, "status": "shipped", "tracking": tracking}


@app.post("/deals/{deal_id}/confirm")
async def deals_confirm(deal_id: str, user: Dict = Depends(get_user)):
    """Buyer confirms receipt → release funds to seller."""
    user_id = int(user["id"])
    now = int(time.time())

    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "deal_not_found"}
        d = _deal_row_to_dict(row)
        if d["buyer_id"] != user_id:
            return {"ok": False, "error": "not_buyer"}
        if d["status"] not in ("shipped", "escrowed"):
            return {"ok": False, "error": f"invalid_status:{d['status']}"}
        result = _deal_settle_release(conn, d)

    # Notify seller
    if bot is not None:
        try:
            amount_text = f"{d['amount_rub']} ₽" if d["currency"] == "RUB" else f"{d['amount_nano'] / TON_NANOTON} TON"
            asyncio.create_task(bot.send_message(
                d["seller_id"],
                f"✅ Сделка #{deal_id} закрыта!\n"
                f"Покупатель подтвердил получение.\n"
                f"Вам зачислено: {amount_text}\n"
                f"Запросите вывод из раздела «Мои сделки» → «Баланс».",
            ))
        except Exception as e:
            logging.warning(f"seller confirm notify failed: {e}")

    return {"ok": True, "deal_id": deal_id, **result}


@app.post("/deals/{deal_id}/dispute")
async def deals_dispute(deal_id: str, request: Request, user: Dict = Depends(get_user)):
    """Buyer or seller opens a dispute. Notifies admin."""
    user_id = int(user["id"])
    try:
        body = await request.json()
        reason = (body.get("reason") or "").strip()
    except Exception:
        reason = ""
    if not reason:
        return {"ok": False, "error": "reason required"}
    now = int(time.time())

    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "deal_not_found"}
        d = _deal_row_to_dict(row)
        if user_id not in (d["buyer_id"], d["seller_id"]):
            return {"ok": False, "error": "not_party"}
        if d["status"] not in ("escrowed", "shipped"):
            return {"ok": False, "error": f"cannot_dispute_in:{d['status']}"}
        conn.execute(
            "UPDATE deals SET status='disputed', dispute_reason=?, closed_at=NULL WHERE id=?",
            (reason, deal_id),
        )
        conn.commit()

    # Notify admin
    if bot is not None:
        try:
            for admin_id in ADMIN_IDS:
                asyncio.create_task(bot.send_message(
                    int(admin_id),
                    f"⚠️ СПОР по сделке #{deal_id}!\n"
                    f"Покупатель: {d['buyer_name']} (@{d['buyer_username'] or '—'})\n"
                    f"Продавец: {d['seller_name']} (@{d['seller_username'] or '—'})\n"
                    f"Сумма: {d['amount_rub']} ₽\n"
                    f"Причина: {reason[:300]}",
                ))
        except Exception as e:
            logging.warning(f"admin dispute notify failed: {e}")

    return {"ok": True, "deal_id": deal_id, "status": "disputed"}


@app.post("/deals/{deal_id}/cancel")
async def deals_cancel(deal_id: str, user: Dict = Depends(get_user)):
    """Cancel deal before payment (awaiting_payment status)."""
    user_id = int(user["id"])
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "deal_not_found"}
        d = _deal_row_to_dict(row)
        if d["buyer_id"] != user_id:
            return {"ok": False, "error": "not_buyer"}
        if d["status"] != "awaiting_payment":
            return {"ok": False, "error": f"cannot_cancel_in:{d['status']}"}
        conn.execute("UPDATE deals SET status='cancelled', closed_at=? WHERE id=?",
                     (int(time.time()), deal_id))
        conn.commit()
    return {"ok": True, "deal_id": deal_id, "status": "cancelled"}


@app.post("/deals/{deal_id}/messages")
async def deals_post_message(deal_id: str, request: Request, user: Dict = Depends(get_user)):
    """Post a message in the deal's chat (buyer ↔ seller)."""
    user_id = int(user["id"])
    try:
        body = await request.json()
        text = (body.get("text") or "").strip()
        photo_url = (body.get("photo_url") or "").strip()
    except Exception:
        text, photo_url = "", ""
    if not text and not photo_url:
        return {"ok": False, "error": "text or photo_url required"}

    now = int(time.time())
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "deal_not_found"}
        d = _deal_row_to_dict(row)
        if user_id not in (d["buyer_id"], d["seller_id"]):
            return {"ok": False, "error": "not_party"}
        conn.execute(
            "INSERT INTO deal_messages (deal_id, from_user_id, text, photo_url, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (deal_id, user_id, text or None, photo_url or None, now),
        )
        conn.commit()
    return {"ok": True, "deal_id": deal_id}


@app.get("/deals/{deal_id}/messages")
async def deals_get_messages(deal_id: str, user: Dict = Depends(get_user)):
    """Get chat messages for a deal (buyer or seller)."""
    user_id = int(user["id"])
    with db_cursor() as conn:
        row = conn.execute("SELECT buyer_id, seller_id FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "deal_not_found"}
        buyer_id = int(row["buyer_id"] if isinstance(row, dict) else row[0])
        seller_id = int(row["seller_id"] if isinstance(row, dict) else row[1])
        if user_id not in (buyer_id, seller_id):
            return {"ok": False, "error": "not_party"}
        msgs = conn.execute(
            "SELECT id, from_user_id, text, photo_url, created FROM deal_messages "
            "WHERE deal_id=? ORDER BY created ASC LIMIT 100",
            (deal_id,),
        ).fetchall()
        out = []
        for m in msgs:
            d = m if isinstance(m, dict) else None
            out.append({
                "id": (d["id"] if d else m[0]),
                "from_user_id": int(d["from_user_id"] if d else m[1]),
                "text": (d["text"] if d else m[2]),
                "photo_url": (d["photo_url"] if d else m[3]),
                "created": int(d["created"] if d else m[4]),
            })
    return {"ok": True, "deal_id": deal_id, "messages": out}


@app.get("/deals/{deal_id}")
async def deals_get(deal_id: str, user: Dict = Depends(get_user)):
    """Get deal details. Buyer, seller, or admin can view."""
    user_id = int(user["id"])
    is_admin = user_id in [int(a) for a in ADMIN_IDS]
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "deal_not_found"}
        d = _deal_row_to_dict(row)
        if not is_admin and user_id not in (d["buyer_id"], d["seller_id"]):
            return {"ok": False, "error": "forbidden"}
    return {"ok": True, "deal": d}


@app.get("/deals")
async def deals_list(request: Request, user: Dict = Depends(get_user)):
    """List deals for current user. ?role=buyer|seller (default both)."""
    user_id = int(user["id"])
    role = request.query_params.get("role", "all")
    with db_cursor() as conn:
        if role == "buyer":
            rows = conn.execute(
                "SELECT * FROM deals WHERE buyer_id=? ORDER BY created DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        elif role == "seller":
            rows = conn.execute(
                "SELECT * FROM deals WHERE seller_id=? ORDER BY created DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM deals WHERE buyer_id=? OR seller_id=? "
                "ORDER BY created DESC LIMIT 50",
                (user_id, user_id),
            ).fetchall()
    return {"ok": True, "deals": [_deal_row_to_dict(r) for r in rows]}


@app.post("/payouts/request")
async def payouts_request(request: Request, user: Dict = Depends(get_user)):
    """Seller requests payout of balance."""
    try:
        body = await request.json()
        currency = (body.get("currency") or "RUB").upper()
        amount = int(body.get("amount") or 0)
        destination = (body.get("destination") or "").strip()
    except Exception as e:
        return {"ok": False, "error": f"bad_request: {e}"}
    if currency not in ("RUB", "TON"):
        return {"ok": False, "error": "currency must be RUB or TON"}
    if amount <= 0:
        return {"ok": False, "error": "amount must be > 0"}
    if not destination:
        return {"ok": False, "error": "destination required"}

    user_id = int(user["id"])
    payout_id = "P-" + _secrets.token_hex(4).upper()
    now = int(time.time())

    with db_cursor() as conn:
        row = conn.execute(
            "SELECT amount FROM seller_balances WHERE user_id=? AND currency=?",
            (user_id, currency),
        ).fetchone()
        cur = int(row["amount"] if isinstance(row, dict) else row[0]) if row else 0
        if cur < amount:
            return {"ok": False, "error": "insufficient_balance", "have": cur, "requested": amount}

        # Deduct balance, create payout
        conn.execute(
            "UPDATE seller_balances SET amount=?, updated=? WHERE user_id=? AND currency=?",
            (cur - amount, now, user_id, currency),
        )
        conn.execute(
            "INSERT INTO payouts (id, user_id, currency, amount, destination, status, created) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (payout_id, user_id, currency, amount, destination, now),
        )
        conn.commit()

    # Notify admin
    if bot is not None:
        try:
            for admin_id in ADMIN_IDS:
                asyncio.create_task(bot.send_message(
                    int(admin_id),
                    f"💸 Заявка на вывод #{payout_id}\n"
                    f"User: {user_id}\n"
                    f"Сумма: {amount} {currency}\n"
                    f"Куда: {destination}",
                ))
        except Exception as e:
            logging.warning(f"admin payout notify failed: {e}")

    return {"ok": True, "payout_id": payout_id, "status": "pending"}


# ============================================================
# Бот-подбиратель (match agent) — AI-агент в личке
# ============================================================
import re as _re

def _parse_match_query(raw_query: str) -> Dict[str, Any]:
    """Parse natural language → structured filters.

    Examples:
      'iPhone 13 до 30К в Москве, чёрный' →
          {keywords:['iphone','13'], cat:'iphone', max_price:30000, city:'Москва', color:'чёрный'}
      'AirPods Pro в идеале' →
          {keywords:['airpods','pro'], cat:'airpods', extra:'в идеале'}
    """
    q = (raw_query or "").strip().lower()
    if not q:
        return {"keywords": [], "cat": None, "max_price": None, "city": None, "color": None, "extra": None}

    # Price: "до 30к", "до 30000", "до 30 000", "< 30к"
    max_price = None
    m = _re.search(r"(?:до|<|макс(?:имум)?\s*)?\s*(\d{1,3}(?:[ \u00a0]?\d{3})*|\d+)\s*к", q)
    if m:
        try:
            n = int(_re.sub(r"[ \u00a0]", "", m.group(1)))
            if n > 100:
                max_price = n  # "до 30к" → 30000? Actually 30к = 30000 already if user typed 30 and 'к'. 30к → 30*1000 = 30000.
            else:
                max_price = n * 1000  # "до 30к" parsed as '30' → 30000
        except Exception:
            pass
    # Override: extract direct big numbers without "к"
    if max_price is None:
        m = _re.search(r"(?:до|<)\s*(\d{4,7})", q)
        if m:
            try:
                max_price = int(m.group(1))
            except Exception:
                pass

    # Cat: detect product family
    cat = None
    cat_map = {
        "iphone": "iphone", "айфон": "iphone",
        "ipad": "ipad", "айпад": "ipad",
        "mac": "mac", "мак": "mac", "macbook": "mac",
        "watch": "watch", "часы": "watch",
        "airpods": "airpods", "наушники": "airpods",
        "аксессуар": "accs", "аксессуары": "accs", "чехол": "accs",
    }
    for kw, c in cat_map.items():
        if kw in q:
            cat = c
            break

    # City: common big cities (Russian) — use stem-prefix matching for declension
    # "Москве", "Москвы", "в Москву" → все мэтчатся со stem "моск"
    city = None
    city_stems = [
        ("Москва", "моск"),
        ("Санкт-Петербург", "петер"),
        ("Санкт-Петербург", "питер"),
        ("Екатеринбург", "екат"),
        ("Казань", "казан"),
        ("Новосибирск", "новос"),
        ("Краснодар", "красн"),
        ("Нижний Новгород", "нижн"),
        ("Самара", "самар"),
        ("Ростов", "росто"),
        ("Уфа", "уфа"),
        ("Челябинск", "челяб"),
    ]
    # Extract words from query and check stem match
    q_words = _re.findall(r"[а-яёa-z]+", q.lower())
    q_stems = set()
    for w in q_words:
        if len(w) >= 4:
            q_stems.add(w[:4])
        else:
            q_stems.add(w)
    # Also check for literal "спб" (3-char abbrev)
    if "спб" in q:
        city = "Санкт-Петербург"
    else:
        for display_name, stem in city_stems:
            if stem in q_stems:
                city = display_name
                break

    # Color
    color = None
    for c in ["чёрный", "черный", "белый", "серый", "синий", "красный", "зелёный", "золотой", "серебристый", "розовый", "фиолетовый"]:
        if c in q:
            color_map = {"чёрный": "чёрный", "черный": "чёрный", "белый": "белый", "серый": "серый", "синий": "синий", "красный": "красный", "зелёный": "зелёный", "золотой": "золотой", "серебристый": "серебристый", "розовый": "розовый", "фиолетовый": "фиолетовый"}
            color = color_map.get(c, c)
            break

    # Keywords: split into tokens, drop price/city/color/cat words + stopwords
    stop = set(["в", "до", "и", "или", "не", "с", "по", "на", "за", "из", "от", "для", "это", "мне", "мне нужен", "мне нужна", "хочу", "ищу", "купить", "продается", "макс", "максимум", "руб", "рублей", "тыс", "тысяч", "идеале", "идеально", "состоянии", "хорошем"])
    # City/color tokens to drop (they're already in dedicated fields)
    cities_drop = set(["москва", "спб", "санкт", "петербург", "екатеринбург", "казань", "новосибирск", "краснодар", "нижний", "новгород", "самара", "ростов", "уфа", "челябинск"])
    colors_drop = set(["чёрный", "черный", "белый", "серый", "синий", "красный", "зелёный", "зелёные", "золотой", "серебристый", "розовый", "фиолетовый"])
    cat_drop = set(["iphone", "айфон", "ipad", "айпад", "mac", "мак", "macbook", "watch", "часы", "airpods", "наушники", "аксессуар", "аксессуары", "чехол"])
    tokens = _re.findall(r"[a-zа-яё0-9]+", q)
    keywords = []
    for t in tokens:
        if t in stop:
            continue
        if t in cities_drop:
            continue
        if t in colors_drop:
            continue
        if t in cat_drop:
            continue
        # Drop price-like tokens: "30", "30к", "30тыс", "30000", "30 000"
        if t.isdigit():
            continue  # any pure number = price fragment
        # Drop tokens with trailing "к"/"тыс" (price shorthand): "30к", "30тыс"
        if _re.match(r"^\d+[кkк]?(тыс)?$", t):
            continue
        if len(t) < 2:
            continue
        keywords.append(t)
    # Dedupe
    seen = set()
    keywords = [k for k in keywords if not (k in seen or seen.add(k))]

    # Extra: words we didn't capture but may be meaningful ("идеале", "без царапин", etc.)
    extras = []
    for phrase in ["без царапин", "в идеале", "в идеальном", "новый", "б/у", "бу", "оригинал", "с коробкой", "с чеком", "гарантия"]:
        if phrase in q:
            extras.append(phrase)

    return {
        "keywords": keywords,
        "cat": cat,
        "max_price": max_price,
        "city": city,
        "color": color,
        "extra": "; ".join(extras) if extras else None,
    }


def _match_listing_to_subscription(filters: Dict[str, Any], listing: Dict[str, Any]) -> bool:
    """Pure-Python matcher (no LLM). Returns True if listing matches filters."""
    # Cat
    if filters.get("cat") and listing.get("cat") != filters["cat"]:
        return False
    # Price
    if filters.get("max_price") is not None:
        price = int(listing.get("price") or 0)
        if price > filters["max_price"]:
            return False
    # City (substring match — listing.city may have district)
    if filters.get("city"):
        fc = filters["city"].lower()
        lc = (listing.get("city") or "").lower()
        if fc not in lc and lc not in fc:
            return False
    # Color — check title + description
    if filters.get("color"):
        text = ((listing.get("title") or "") + " " + (listing.get("description") or "")).lower()
        if filters["color"].lower() not in text:
            return False
    # Keywords: require ALL keywords to appear in title+description
    # Uses Russian stem-prefix matching to handle declension: "москва" matches "москве", "москвы", "москвой"
    keywords = filters.get("keywords") or []
    if keywords:
        text = ((listing.get("title") or "") + " " + (listing.get("description") or "")).lower()
        text_words = _re.findall(r"[а-яёa-z0-9]+", text)
        # Build prefix set (first 4 chars of each word) for declension-tolerant matching
        text_prefixes = set()
        for w in text_words:
            if len(w) >= 4:
                text_prefixes.add(w[:4])
            elif len(w) >= 2:
                text_prefixes.add(w)
        for kw in keywords:
            kl = kw.lower()
            if kl in text:
                continue  # exact substring match
            # Try stem-prefix match (handles Russian declension)
            kp = kl[:4] if len(kl) >= 4 else kl
            if kp and kp in text_prefixes:
                continue
            return False
    return True


async def _notify_match_subscribers(listing_id: str, item: ListingIn, user: Dict[str, Any], msg_id: int):
    """Called by post_to_channel after a successful publish.
    Notifies all active subscriptions whose filters match this listing.
    Rate-limited: each (subscription, listing) pair only fires once.
    """
    now = int(time.time())
    listing_dict = {
        "id": listing_id,
        "title": item.title,
        "description": getattr(item, "description", "") or "",
        "price": item.price,
        "cat": item.cat,
        "city": item.city,
        "tier": item.tier,
    }

    # Fetch all active subs (small table, OK to scan; add idx on active=1 if grows)
    try:
        with db_cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM match_subscriptions WHERE active=1"
            ).fetchall()
    except Exception as e:
        logger.warning(f"match: failed to fetch subs: {e}")
        return

    if not rows:
        return

    # Iterate
    matched_user_ids = set()
    for row in rows:
        # Dict/tuple agnostic access
        def _g(r, k, idx):
            try:
                if hasattr(r, "keys"):
                    return r[k]
                return r[idx]
            except Exception:
                return None
        sd = {
            "id": _g(row, "id", 0),
            "user_id": _g(row, "user_id", 1),
            "keywords": _g(row, "keywords", 5),
            "cat": _g(row, "cat", 6),
            "max_price_rub": _g(row, "max_price_rub", 7),
            "city": _g(row, "city", 8),
            "color": _g(row, "color", 9),
            "last_notified": _g(row, "last_notified", 14),
        }
        try:
            kw_list = [k.strip() for k in (sd["keywords"] or "").split(",") if k.strip()]
        except Exception:
            kw_list = []
        filters = {
            "cat": sd["cat"],
            "max_price": int(sd["max_price_rub"]) if sd["max_price_rub"] is not None else None,
            "city": sd["city"],
            "color": sd["color"],
            "keywords": kw_list,
        }
        if not _match_listing_to_subscription(filters, listing_dict):
            continue
        # Already notified about this listing?
        try:
            with db_cursor() as conn:
                dup = conn.execute(
                    "SELECT id FROM match_log WHERE subscription_id=? AND listing_id=?",
                    (sd["id"], listing_id),
                ).fetchone()
                if dup:
                    continue
        except Exception:
            pass
        # Rate-limit per-sub: skip if notified <60s ago
        if sd["last_notified"] and (now - int(sd["last_notified"])) < 60:
            continue
        # Send push
        uid = int(sd["user_id"])
        matched_user_ids.add(uid)
        await _push_match(uid, sd["id"], listing_id, listing_dict, msg_id)
        # Log + update last_notified
        try:
            with db_cursor() as conn:
                conn.execute(
                    "INSERT INTO match_log (subscription_id, listing_id, sent_at) VALUES (?,?,?)",
                    (sd["id"], listing_id, now),
                )
                conn.execute(
                    "UPDATE match_subscriptions SET last_notified=? WHERE id=?",
                    (now, sd["id"]),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"match: failed to log for {sd['id']}: {e}")


async def _push_match(user_id: int, sub_id: str, listing_id: str, listing: Dict[str, Any], msg_id: int):
    """Send push notification to user about matching listing."""
    if not BOT_TOKEN:
        return
    try:
        price_str = f"{listing['price']:,}".replace(",", " ") + " ₽"
        text = (
            f"🔔 <b>Нашёл по твоему запросу!</b>\n\n"
            f"<b>{listing['title']}</b>\n"
            f"💰 {price_str}\n"
            f"📍 {listing.get('city','')}\n\n"
            f"Открыть: https://ibaraholka.p.spru.io/"
        )
        kb = {
            "inline_keyboard": [
                [{"text": "👀 Посмотреть", "url": f"https://t.me/ibaraholkatyt/{msg_id}"}],
                [{"text": "🔕 Отключить эту подписку", "callback_data": f"match_stop:{sub_id}"}],
            ]
        }
        payload = {
            "chat_id": str(user_id),
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(kb),
            "disable_web_page_preview": "true",
        }
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        if not result.get("ok"):
            err = result.get("description", "")
            if "blocked" in err.lower() or "deactivated" in err.lower() or "chat not found" in err.lower():
                # User blocked bot — deactivate all their subs
                try:
                    with db_cursor() as conn:
                        conn.execute("UPDATE match_subscriptions SET active=0 WHERE user_id=?", (user_id,))
                        conn.commit()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"push_match failed for user {user_id}: {e}")



@app.post("/match/subscribe")
async def match_subscribe(request: Request, user: Dict = Depends(get_user)):
    """Create a new match subscription.

    Body: {query: str, payment_method?: 'free'|'coins'|'stars'}
    - 1-я активная подписка — бесплатно навсегда
    - Дальше: 5 IB Coins (списание с user_balances) или 50 Stars через Telegram invoice
    """
    body = await request.json()
    raw_query = (body.get("query") or "").strip()
    payment_method = body.get("payment_method", "free")
    if not raw_query:
        return {"ok": False, "error": "empty_query"}
    if len(raw_query) > 500:
        return {"ok": False, "error": "query_too_long"}

    user_id = int(user["id"])
    parsed = _parse_match_query(raw_query)
    if not parsed["keywords"] and not parsed["cat"]:
        return {"ok": False, "error": "could_not_parse", "hint": "Укажи товар (iPhone/AirPods/iPad/Mac) или конкретную модель"}

    # Check existing subs to decide free vs paid
    now = int(time.time())
    sub_id = "MS-" + _uuid4().hex[:6].upper()
    is_free = False
    try:
        with db_cursor() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c, SUM(is_free) AS f FROM match_subscriptions WHERE user_id=? AND active=1",
                (user_id,),
            ).fetchone()
            def _gcnt(r, k, idx):
                if r is None: return 0
                if isinstance(r, dict): return r.get(k) or 0
                try: return r[k]
                except (KeyError, IndexError): return r[idx] if idx < len(r) else 0
            cnt = int(_gcnt(row, "c", 0) or 0)
            free_cnt = int(_gcnt(row, "f", 1) or 0)
    except Exception:
        cnt = 0
        free_cnt = 0

    # 1st active subscription is free
    if cnt == 0:
        is_free = True
    elif free_cnt > 0:
        # user already used their free one
        is_free = False
    else:
        is_free = False

    # For paid: charge coins / stars
    if not is_free:
        if payment_method == "coins":
            # Check + deduct 5 coins
            try:
                with db_cursor() as conn:
                    bal = conn.execute(
                        "SELECT coins FROM user_balances WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    have = int(bal["coins"]) if bal and bal.get("coins") is not None else 0
                    if have < 5:
                        return {"ok": False, "error": "insufficient_coins", "have": have, "need": 5,
                                "hint": "Посмотри рекламу в Mini App или пополни баланс"}
                    conn.execute(
                        "UPDATE user_balances SET coins=coins-5, updated=? WHERE user_id=?",
                        (now, user_id),
                    )
                    conn.commit()
            except Exception as e:
                return {"ok": False, "error": f"coins_charge_failed: {e}"}
        elif payment_method == "stars":
            # Stars handled client-side via Telegram invoice; we just create paid sub
            pass  # actual deduction via successful_payment → /match/stars-confirm
        else:
            return {"ok": False, "error": "payment_required", "need_payment": True, "free_used": free_cnt > 0}

    # Create subscription
    try:
        with db_cursor() as conn:
            conn.execute(
                "INSERT INTO match_subscriptions "
                "(id, user_id, user_name, user_username, query, keywords, cat, max_price_rub, city, color, extra, active, is_free, paid_until, created, last_notified) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,NULL)",
                (sub_id, user_id, user.get("first_name", ""), user.get("username", ""),
                 raw_query, ",".join(parsed["keywords"]), parsed["cat"], parsed["max_price"],
                 parsed["city"], parsed["color"], parsed["extra"], 1 if is_free else 0,
                 (now + 30*86400) if not is_free else None, now),
            )
            conn.commit()
    except Exception as e:
        import traceback
        logger.error(f"match_subscribe create_failed: {e}\n{traceback.format_exc()}")
        return {"ok": False, "error": f"create_failed: {e}", "tb": traceback.format_exc()}

    return {"ok": True, "subscription_id": sub_id, "filters": parsed,
            "is_free": is_free, "message": "Подписка создана — буду присылать подходящие объявления"}


@app.get("/match/subscriptions")
async def match_list(user: Dict = Depends(get_user)):
    """List current user's match subscriptions."""
    user_id = int(user["id"])
    try:
        with db_cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM match_subscriptions WHERE user_id=? ORDER BY created DESC",
                (user_id,),
            ).fetchall()
        out = []
        for row in rows:
            sd = dict(row) if hasattr(row, "keys") else {}
            sd["active"] = bool(sd.get("active"))
            sd["is_free"] = bool(sd.get("is_free"))
            out.append(sd)
        return {"ok": True, "subscriptions": out, "count": len(out)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.delete("/match/subscriptions/{sub_id}")
async def match_unsubscribe(sub_id: str, user: Dict = Depends(get_user)):
    """Deactivate a subscription (soft-delete)."""
    user_id = int(user["id"])
    try:
        with db_cursor() as conn:
            row = conn.execute(
                "SELECT user_id FROM match_subscriptions WHERE id=?", (sub_id,)
            ).fetchone()
            if not row:
                return {"ok": False, "error": "not_found"}
            owner = int(row["user_id"]) if hasattr(row, "keys") else int(row[0])
            if owner != user_id:
                return {"ok": False, "error": "not_owner"}
            conn.execute("UPDATE match_subscriptions SET active=0 WHERE id=?", (sub_id,))
            conn.commit()
        return {"ok": True, "subscription_id": sub_id, "status": "deactivated"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================
# REFERRALS — Реф-лесенка: «Приведи 5 друзей — 1 день VIP, 25 — неделя»
# ============================================================
# Сценарий:
#   - Юзер открывает Mini App → видит свою реф-ссылку → делится с друзьями
#   - Друг кликает → t.me/Ibaraholka_bot?startapp=ref_<user_id>
#   - Mini App при первом запуске отправляет /referrals/track с referred_id
#   - Бэкенд пишет в таблицу referrals, проверяет milestone, выдаёт бонусы
#
# Milestones:
#   1 реф   → 5 IB Coins (новый юзер) + 5 IB Coins (реферер)
#   3 рефа  → 10 IB Coins (реферер)
#   5 рефов → 1 день VIP
#   15 рефов → 3 дня VIP
#   25 рефов → 7 дней VIP

# Минимальный ID юзера для реф-ссылки (защита от мусора)
MIN_REFERRER_ID = 1000
MIN_REFERRED_ID = 1000

REFERRAL_MILESTONES = [
    # (count, type, value, description)
    (1, "coins", "5", "5 IB Coins за каждого друга"),
    (3, "coins", "10", "10 IB Coins бонус"),
    (5, "vip_days", "1", "1 день VIP"),
    (15, "vip_days", "3", "3 дня VIP"),
    (25, "vip_days", "7", "7 дней VIP"),
]


def _grant_milestone_bonus(conn, user_id: int, milestone: int, bonus_type: str, bonus_value: str):
    """Начислить бонус юзеру. Идемпотентно через UNIQUE(user_id, milestone)."""
    now = int(time.time())
    try:
        # Check if already granted
        existing = conn.execute(
            "SELECT id FROM referral_bonuses WHERE user_id=? AND milestone=?",
            (user_id, milestone),
        ).fetchone()
        if existing:
            return False  # уже выдан

        # Record the grant
        conn.execute(
            "INSERT INTO referral_bonuses (user_id, milestone, bonus_type, bonus_value, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, milestone, bonus_type, bonus_value, now),
        )

        if bonus_type == "coins":
            coins = int(bonus_value)
            # UPSERT user_balances
            conn.execute(
                "INSERT INTO user_balances (user_id, coins, total_earned, total_spent, updated) "
                "VALUES (?, ?, ?, 0, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET coins = coins + ?, total_earned = total_earned + ?, updated = ?",
                (user_id, coins, coins, now, coins, coins, now),
            )
            return ("coins", coins)

        elif bonus_type == "vip_days":
            days = int(bonus_value)
            # Set VIP until now+days*86400 in user_balances (or create profile)
            # We use a separate vip_until field in user_balances (need schema check)
            # For now: credit via listings tier=free→vip auto-apply for next listing
            # Simplest: just store vip_until timestamp
            conn.execute(
                "ALTER TABLE user_balances ADD COLUMN IF NOT EXISTS vip_until INTEGER DEFAULT 0",
            )
            # Get current vip_until (max with new)
            existing_balance = conn.execute(
                "SELECT vip_until, coins, total_earned, total_spent FROM user_balances WHERE user_id=?",
                (user_id,),
            ).fetchone()
            cur_vip = 0
            cur_coins = 0
            cur_earned = 0
            cur_spent = 0
            if existing_balance:
                if isinstance(existing_balance, dict):
                    cur_vip = int(existing_balance.get("vip_until") or 0)
                    cur_coins = int(existing_balance.get("coins") or 0)
                    cur_earned = int(existing_balance.get("total_earned") or 0)
                    cur_spent = int(existing_balance.get("total_spent") or 0)
                else:
                    cur_vip = int(existing_balance[0] or 0)
                    cur_coins = int(existing_balance[1] or 0)
                    cur_earned = int(existing_balance[2] or 0)
                    cur_spent = int(existing_balance[3] or 0)
            base = max(now, cur_vip)
            new_vip = base + days * 86400
            conn.execute(
                "INSERT INTO user_balances (user_id, coins, total_earned, total_spent, updated, vip_until) "
                "VALUES (?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET vip_until = ?, updated = ?",
                (user_id, cur_coins, cur_earned, now, new_vip, new_vip, now),
            )
            return ("vip_days", days, new_vip)

    except Exception as e:
        logger.error(f"_grant_milestone_bonus error: {e}")
        return False


def _check_and_grant_milestones(conn, referrer_id: int):
    """Check current referral count for referrer and grant any new milestones."""
    granted = []
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?",
            (referrer_id,),
        ).fetchone()
        cnt = 0
        if row:
            cnt = int(row["c"] if isinstance(row, dict) else row[0])
        for milestone, btype, bvalue, _ in REFERRAL_MILESTONES:
            if cnt >= milestone:
                result = _grant_milestone_bonus(conn, referrer_id, milestone, btype, bvalue)
                if result:
                    granted.append({"milestone": milestone, "type": btype, "value": bvalue})
    except Exception as e:
        logger.error(f"_check_and_grant_milestones error: {e}")
    return granted


@app.post("/referrals/track")
async def referrals_track(request: Request):
    """Mini App calls this on first launch to register a referral.

    Called BEFORE auth resolves (because the ref_user is what we want to track
    even before user opens app). Body: {referrer_id, referred_id, referred_username?, referred_first_name?}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        referrer_id = int(body.get("referrer_id") or 0)
        referred_id = int(body.get("referred_id") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad_ids"}
    if referrer_id < MIN_REFERRER_ID or referred_id < MIN_REFERRER_ID:
        return {"ok": False, "error": "invalid_ids"}
    if referrer_id == referred_id:
        return {"ok": False, "error": "self_referral"}
    referred_username = (body.get("referred_username") or "").strip()[:64]
    referred_first_name = (body.get("referred_first_name") or "").strip()[:64]
    now = int(time.time())
    try:
        with db_cursor() as conn:
            # UNIQUE(referred_id) constraint — INSERT OR IGNORE
            cur = conn.execute(
                "INSERT INTO referrals (referrer_id, referred_id, referred_username, referred_first_name, created) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(referred_id) DO NOTHING RETURNING id",
                (referrer_id, referred_id, referred_username, referred_first_name, now),
            )
            new_id = cur.fetchone() if hasattr(cur, "fetchone") else None
            inserted = bool(new_id)
            # Get current count + bonuses
            cnt_row = conn.execute(
                "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?",
                (referrer_id,),
            ).fetchone()
            cnt = int(cnt_row["c"] if isinstance(cnt_row, dict) else cnt_row[0])
            # Check + grant milestones
            granted = _check_and_grant_milestones(conn, referrer_id)
            conn.commit()
        # Notify referrer
        if inserted and bot is not None:
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 <b>Новый реферал!</b>\n\n"
                    f"Кто-то пришёл по твоей ссылке. У тебя уже <b>{cnt}</b> приглашённых.\n\n"
                    f"Награды:\n"
                    + "\n".join([f"— {m} реф → {desc}" for m, _, _, desc in REFERRAL_MILESTONES])
                    + f"\n\n<i>Открой Mini App → 🎁 Реф-лесенка</i>",
                )
            except Exception as e:
                logger.warning(f"referral notify failed: {e}")
        return {
            "ok": True,
            "inserted": inserted,
            "referrer_id": referrer_id,
            "referred_id": referred_id,
            "referrals_count": cnt,
            "bonuses_granted": granted,
            "next_milestone": next(
                ({"count": m, "type": bt, "value": bv, "desc": d}
                 for m, bt, bv, d in REFERRAL_MILESTONES if cnt < m),
                None,
            ),
        }
    except Exception as e:
        logger.error(f"/referrals/track error: {e}")
        return {"ok": False, "error": str(e)}


@app.get("/referrals/stats")
async def referrals_stats(user: Dict = Depends(get_user)):
    """Get referral stats for current user."""
    user_id = int(user["id"])
    try:
        with db_cursor() as conn:
            cnt_row = conn.execute(
                "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?",
                (user_id,),
            ).fetchone()
            cnt = int(cnt_row["c"] if isinstance(cnt_row, dict) else cnt_row[0])
            bonuses_rows = conn.execute(
                "SELECT milestone, bonus_type, bonus_value, created FROM referral_bonuses "
                "WHERE user_id=? ORDER BY milestone",
                (user_id,),
            ).fetchall()
            referred_rows = conn.execute(
                "SELECT referred_id, referred_username, referred_first_name, created "
                "FROM referrals WHERE referrer_id=? ORDER BY created DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        bonuses = []
        for r in bonuses_rows:
            d = r if isinstance(r, dict) else None
            bonuses.append({
                "milestone": int(d["milestone"] if d else r[0]),
                "type": d["bonus_type"] if d else r[1],
                "value": d["bonus_value"] if d else r[2],
                "created": int(d["created"] if d else r[3]),
            })
        referred = []
        for r in referred_rows:
            d = r if isinstance(r, dict) else None
            referred.append({
                "user_id": int(d["referred_id"] if d else r[0]),
                "username": d["referred_username"] if d else r[1],
                "first_name": d["referred_first_name"] if d else r[2],
                "created": int(d["created"] if d else r[3]),
            })
        return {
            "ok": True,
            "referrals_count": cnt,
            "bonuses": bonuses,
            "referred": referred,
            "next_milestone": next(
                ({"count": m, "type": bt, "value": bv, "desc": d}
                 for m, bt, bv, d in REFERRAL_MILESTONES if cnt < m),
                None,
            ),
            "milestones": [
                {"count": m, "type": bt, "value": bv, "desc": d}
                for m, bt, bv, d in REFERRAL_MILESTONES
            ],
            "referrer_id": user_id,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/referrals/claim")
async def referrals_claim(request: Request, user: Dict = Depends(get_user)):
    """Manually claim any pending milestones. Normally auto-claimed on each /track."""
    user_id = int(user["id"])
    try:
        with db_cursor() as conn:
            granted = _check_and_grant_milestones(conn, user_id)
            conn.commit()
        return {"ok": True, "granted": granted}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Bot command: /refs — show stats in DM
@dp.message(Command("refs"))
async def cmd_refs(message: types.Message):
    """Show user's referral ladder progress."""
    user_id = int(message.from_user.id)
    try:
        with db_cursor() as conn:
            cnt_row = conn.execute(
                "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=?",
                (user_id,),
            ).fetchone()
            cnt = int(cnt_row["c"] if isinstance(cnt_row, dict) else cnt_row[0])
        # Build milestone ladder
        lines = []
        for m, btype, bval, desc in REFERRAL_MILESTONES:
            mark = "✅" if cnt >= m else "🔒"
            lines.append(f"{mark} <b>{m}</b> — {desc}")
        ladder = "\n".join(lines)
        await message.answer(
            f"🎁 <b>Реф-лесенка</b>\n\n"
            f"Ты привёл: <b>{cnt}</b> друзей\n\n"
            f"{ladder}\n\n"
            f"📤 Твоя ссылка:\n"
            f"<code>https://t.me/Ibaraholka_bot?startapp=ref_{user_id}</code>\n\n"
            f"<i>Кидай друзьям — за каждого получишь бонус. Ссылка работает и в личке бота, и в Mini App.</i>",
        )
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")


# Bot start handler — detect ?start=ref_XXX or ?startapp=ref_XXX and show bonus info
# (Existing @dp.message(CommandStart()) above; we extend via @dp.message(Command("start"))? No — use F.text starts-with check.)
# We'll register a separate filter to catch the ref payload before the generic start handler.
# Actually simplest: extend the existing cmd_start to show a bonus hint if payload starts with 'ref_'.


# ============================================================
# Run: bot (polling) + API (uvicorn) in same process
# ============================================================


@app.get("/seller/balance")
async def seller_balance_get(user: Dict = Depends(get_user)):
    """Get current seller's balance (RUB + TON)."""
    user_id = int(user["id"])
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT currency, amount FROM seller_balances WHERE user_id=?",
            (user_id,),
        ).fetchall()
        rub = 0
        ton = 0
        for r in rows:
            d = r if isinstance(r, dict) else None
            cur = (d["currency"] if d else r[0])
            amt = int(d["amount"] if d else r[1])
            if cur == "RUB":
                rub = amt
            elif cur == "TON":
                ton = amt
    return {"ok": True, "rub_kopeyki": rub, "rub": rub / 100, "ton_nano": ton, "ton": ton / TON_NANOTON}


@app.post("/admin/deals/{deal_id}/resolve")
async def admin_deals_resolve(deal_id: str, request: Request, x_admin_token: str = Header(None, alias="x-admin-token")):
    """Admin resolves a disputed deal: released (seller wins) or refunded (buyer wins)."""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")
    try:
        body = await request.json()
        outcome = (body.get("outcome") or "").strip()  # 'release' or 'refund'
        note = (body.get("note") or "").strip()
    except Exception:
        outcome, note = "", ""
    if outcome not in ("release", "refund"):
        return {"ok": False, "error": "outcome must be release|refund"}

    now = int(time.time())
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "deal_not_found"}
        d = _deal_row_to_dict(row)
        if d["status"] != "disputed":
            return {"ok": False, "error": f"not_disputed:{d['status']}"}
        if outcome == "release":
            result = _deal_settle_release(conn, d)
            conn.execute(
                "UPDATE deals SET dispute_resolution=? WHERE id=?",
                (f"RELEASED: {note}" if note else "RELEASED", deal_id),
            )
        else:
            # Refund: credit buyer (for now just log — admin handles actual refund manually)
            conn.execute(
                "UPDATE deals SET status='refunded', closed_at=?, dispute_resolution=? WHERE id=?",
                (now, f"REFUNDED: {note}" if note else "REFUNDED", deal_id),
            )
            conn.commit()
            result = {"status": "refunded"}

    # Notify both parties
    if bot is not None:
        try:
            text = (
                f"✅ Спор по сделке #{deal_id} решён в пользу продавца. Деньги зачислены."
                if outcome == "release"
                else f"↩️ Спор по сделке #{deal_id} решён в пользу покупателя. Деньги возвращены."
            )
            for uid in (d["buyer_id"], d["seller_id"]):
                asyncio.create_task(bot.send_message(int(uid), text))
        except Exception as e:
            logging.warning(f"deal resolve notify failed: {e}")

    return {"ok": True, "deal_id": deal_id, **result}


@app.get("/admin/deals")
async def admin_deals_list(request: Request, status: str = "disputed",
                            x_admin_token: str = Header(None, alias="x-admin-token")):
    """Admin: list deals (optionally filter by status)."""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")
    with db_cursor() as conn:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM deals ORDER BY created DESC LIMIT 100"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM deals WHERE status=? ORDER BY created DESC LIMIT 100",
                (status,),
            ).fetchall()
    return {"ok": True, "deals": [_deal_row_to_dict(r) for r in rows]}


@app.get("/admin/payouts")
async def admin_payouts_list(x_admin_token: str = Header(None, alias="x-admin-token"),
                              status: str = "pending"):
    """Admin: list payout requests."""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, user_id, currency, amount, destination, status, tx_hash, created, completed, note "
            "FROM payouts WHERE (?='all' OR status=?) ORDER BY created DESC LIMIT 100",
            (status, status),
        ).fetchall()
        out = []
        for r in rows:
            d = r if isinstance(r, dict) else None
            out.append({
                "id": (d["id"] if d else r[0]),
                "user_id": int(d["user_id"] if d else r[1]),
                "currency": (d["currency"] if d else r[2]),
                "amount": int(d["amount"] if d else r[3]),
                "destination": (d["destination"] if d else r[4]),
                "status": (d["status"] if d else r[5]),
                "tx_hash": (d["tx_hash"] if d else r[6]),
                "created": int(d["created"] if d else r[7]),
                "completed": int(d["completed"] if d else r[8]) if (d["completed"] if d else r[8]) else None,
                "note": (d["note"] if d else r[9]),
            })
    return {"ok": True, "payouts": out}


@app.post("/admin/payouts/{payout_id}/complete")
async def admin_payouts_complete(payout_id: str, request: Request,
                                  x_admin_token: str = Header(None, alias="x-admin-token")):
    """Admin marks payout as completed and attaches tx_hash / external transfer ref."""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")
    try:
        body = await request.json()
        tx_hash = (body.get("tx_hash") or "").strip()
        note = (body.get("note") or "").strip()
    except Exception:
        tx_hash, note = "", ""
    if not tx_hash and not note:
        return {"ok": False, "error": "tx_hash or note required"}
    now = int(time.time())
    with db_cursor() as conn:
        row = conn.execute("SELECT user_id, currency, amount FROM payouts WHERE id=?",
                          (payout_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "payout_not_found"}
        d = row if isinstance(row, dict) else None
        user_id = int(d["user_id"] if d else row[0])
        conn.execute(
            "UPDATE payouts SET status='completed', completed=?, tx_hash=?, note=? WHERE id=?",
            (now, tx_hash or None, note or None, payout_id),
        )
        conn.commit()
    # Notify seller
    if bot is not None:
        try:
            asyncio.create_task(bot.send_message(
                user_id,
                f"✅ Выплата #{payout_id} выполнена админом. {note or ''}",
            ))
        except Exception as e:
            logging.warning(f"payout notify failed: {e}")
    return {"ok": True, "payout_id": payout_id, "status": "completed"}


# ============================================================
# FAVORITES — Избранное
# ============================================================
def _row_to_dict(r, cols):
    """Convert DB row to dict (dict/tuple agnostic)."""
    if r is None:
        return None
    try:
        if hasattr(r, "keys"):
            return {k: r[k] for k in cols}
    except Exception:
        pass
    return dict(zip(cols, r))


@app.get("/favorites")
async def favorites_list(user: Dict[str, Any] = Depends(get_user)):
    """Список избранных объявлений пользователя."""
    uid = int(user["id"])
    cols = ["id", "user_id", "user_name", "user_username", "title", "description", "price", "cat", "type", "contact", "photo", "tier", "city", "status", "created", "expires_at", "channel_message_id", "paid_at"]
    with db_cursor() as conn:
        rows = conn.execute("SELECT l.* FROM listings l JOIN favorites f ON f.listing_id = l.id WHERE f.user_id = ? ORDER BY f.created DESC LIMIT 200", (uid,)).fetchall()
        items = [_row_to_dict(r, cols) for r in rows]
    return {"ok": True, "favorites": items, "count": len(items)}


@app.post("/favorites/{listing_id}")
async def favorites_add(listing_id: str, user: Dict[str, Any] = Depends(get_user)):
    """Добавить объявление в избранное."""
    uid = int(user["id"])
    now = int(time.time())
    with db_cursor() as conn:
        row = conn.execute("SELECT id FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        conn.execute(
            "INSERT INTO favorites (user_id, listing_id, created) VALUES (?, ?, ?) ON CONFLICT (user_id, listing_id) DO NOTHING",
            (uid, listing_id, now)
        )
    return {"ok": True, "listing_id": listing_id}


@app.delete("/favorites/{listing_id}")
async def favorites_remove(listing_id: str, user: Dict[str, Any] = Depends(get_user)):
    """Удалить объявление из избранного."""
    uid = int(user["id"])
    with db_cursor() as conn:
        conn.execute("DELETE FROM favorites WHERE user_id = ? AND listing_id = ?", (uid, listing_id))
    return {"ok": True, "listing_id": listing_id}


# ============================================================
# REVIEWS — Отзывы на продавца
# ============================================================
@app.post("/reviews")
async def reviews_create(request: Request, user: Dict[str, Any] = Depends(get_user)):
    """Оставить отзыв на продавца. Требуется завершённая сделка."""
    body = await request.json()
    deal_id = body.get("deal_id")
    rating = body.get("rating")
    text = (body.get("text") or "").strip()[:500]
    if not deal_id or rating is None:
        raise HTTPException(400, "deal_id and rating required")
    if not (1 <= int(rating) <= 5):
        raise HTTPException(400, "rating must be 1..5")
    uid = int(user["id"])
    now = int(time.time())
    with db_cursor() as conn:
        deal_row = conn.execute(
            "SELECT id, seller_id, buyer_id, status FROM deals WHERE id = ?",
            (deal_id,)
        ).fetchone()
        if not deal_row:
            raise HTTPException(404, "Deal not found")
        deal = _row_to_dict(deal_row, ["id", "seller_id", "buyer_id", "status"])
        if int(deal["buyer_id"]) != uid:
            raise HTTPException(403, "Not your deal")
        if deal["status"] not in ("released", "completed"):
            raise HTTPException(400, f"Deal not completed (status={deal['status']})")
        seller_id = int(deal["seller_id"])
        try:
            conn.execute(
                "INSERT INTO reviews (deal_id, seller_id, buyer_id, rating, text, created) VALUES (?, ?, ?, ?, ?, ?)",
                (deal_id, seller_id, uid, int(rating), text, now)
            )
        except Exception as e:
            if "UNIQUE" in str(e) or "duplicate" in str(e).lower():
                raise HTTPException(400, "Review already exists")
            raise
    return {"ok": True, "deal_id": deal_id, "rating": int(rating)}


@app.get("/users/{user_id}/reviews")
async def user_reviews(user_id: int):
    """Все отзывы на продавца + средний рейтинг."""
    cols = ["id", "deal_id", "buyer_id", "rating", "text", "created"]
    with db_cursor() as conn:
        rows = conn.execute("SELECT id, deal_id, buyer_id, rating, text, created FROM reviews WHERE seller_id = ? ORDER BY created DESC LIMIT 100", (user_id,)).fetchall()
        items = [_row_to_dict(r, cols) for r in rows]
        avg = (sum(r["rating"] for r in items) / len(items)) if items else 0.0
        return {"ok": True, "seller_id": user_id, "avg_rating": round(avg, 2), "count": len(items), "reviews": items}


# ============================================================
# LISTING VIEWS — Просмотры + "X человек смотрят"
# ============================================================
@app.post("/listings/{listing_id}/view")
async def listing_view(listing_id: str, user: Dict[str, Any] = Depends(get_user)):
    """Засчитать просмотр объявления (для FOMO-счётчика)."""
    uid = int(user["id"])
    now = int(time.time())
    with db_cursor() as conn:
        row = conn.execute("SELECT id FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        # антинакрутка: один юзер = 1 просмотр в 5 минут
        recent = conn.execute(
            "SELECT id FROM listing_views WHERE listing_id = ? AND viewer_id = ? AND created > ?",
            (listing_id, uid, now - 300)
        ).fetchone()
        if not recent:
            conn.execute(
                "INSERT INTO listing_views (listing_id, viewer_id, created) VALUES (?, ?, ?)",
                (listing_id, uid, now)
            )
        watchers_n = conn.execute(
            "SELECT COUNT(DISTINCT viewer_id) AS n FROM listing_views WHERE listing_id = ? AND created > ?",
            (listing_id, now - 300)
        ).fetchone()
        n = int(watchers_n["n"]) if watchers_n else 0
        # FOMO-число
        if n >= 5:
            display = max(n, 5)
        elif n >= 2:
            display = n
        else:
            display = 0
    return {"ok": True, "listing_id": listing_id, "watching_now": display, "real_watchers": n}


@app.get("/listings/{listing_id}/stats")
async def listing_stats(listing_id: str):
    """Статистика объявления для продавца: просмотры за 24ч/7д/всего."""
    now = int(time.time())
    with db_cursor() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM listing_views WHERE listing_id = ?", (listing_id,)).fetchone()
        last_24h = conn.execute("SELECT COUNT(*) AS n FROM listing_views WHERE listing_id = ? AND created > ?", (listing_id, now - 86400)).fetchone()
        last_7d = conn.execute("SELECT COUNT(*) AS n FROM listing_views WHERE listing_id = ? AND created > ?", (listing_id, now - 604800)).fetchone()
    return {
        "ok": True,
        "listing_id": listing_id,
        "views_total": int(total["n"]) if total else 0,
        "views_24h": int(last_24h["n"]) if last_24h else 0,
        "views_7d": int(last_7d["n"]) if last_7d else 0,
    }


# ============================================================
# SEARCH — Полнотекстовый поиск объявлений
# ============================================================
@app.get("/search")
async def search(q: str = "", cat: str = "", city: str = "", max_price: int = 0, limit: int = 50):
    """Поиск объявлений. q ищет по title+description (case-insensitive LIKE)."""
    limit = min(max(limit, 1), 100)
    where = ["status = 'active'"]
    params = []
    if q.strip():
        where.append("(LOWER(title) LIKE ? OR LOWER(description) LIKE ?)")
        ql = f"%{q.strip().lower()}%"
        params.extend([ql, ql])
    if cat and cat != "all":
        where.append("cat = ?")
        params.append(cat)
    if city.strip():
        where.append("LOWER(city) LIKE ?")
        params.append(f"%{city.strip().lower()}%")
    if max_price > 0:
        where.append("price <= ?")
        params.append(max_price)
    sql = f"SELECT id, user_id, user_name, user_username, title, description, price, cat, type, contact, photo, tier, city, status, created, expires_at, channel_message_id, paid_at FROM listings WHERE {' AND '.join(where)} ORDER BY created DESC LIMIT {limit}"
    cols = ["id", "user_id", "user_name", "user_username", "title", "description", "price", "cat", "type", "contact", "photo", "tier", "city", "status", "created", "expires_at", "channel_message_id", "paid_at"]
    try:
        with db_cursor() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            items = [_row_to_dict(r, cols) for r in rows]
    except Exception as e:
        logger.exception(f"/search failed: {e}")
        return {"ok": False, "error": "search_failed", "msg": str(e)[:200]}
    return {"ok": True, "q": q, "count": len(items), "listings": items}


# ============================================================
# SAVED FILTERS — Сохранённые фильтры
# ============================================================
@app.post("/saved-filters")
async def saved_filters_save(request: Request, user: Dict[str, Any] = Depends(get_user)):
    """Сохранить набор фильтров под именем."""
    body = await request.json()
    name = (body.get("name") or "").strip()[:50]
    if not name:
        raise HTTPException(400, "name required")
    uid = int(user["id"])
    now = int(time.time())
    cat = (body.get("cat") or "").strip() or None
    city = (body.get("city") or "").strip() or None
    max_price = int(body.get("max_price") or 0) or None
    query = (body.get("query") or "").strip() or None
    with db_cursor() as conn:
        conn.execute(
            "INSERT INTO saved_filters (user_id, name, cat, city, max_price, query, created) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, name) DO UPDATE SET cat=EXCLUDED.cat, city=EXCLUDED.city, max_price=EXCLUDED.max_price, query=EXCLUDED.query",
            (uid, name, cat, city, max_price, query, now)
        )
    return {"ok": True, "name": name}


@app.get("/saved-filters")
async def saved_filters_list(user: Dict[str, Any] = Depends(get_user)):
    """Список сохранённых фильтров пользователя."""
    uid = int(user["id"])
    with db_cursor() as conn:
        rows = conn.execute("SELECT id, name, cat, city, max_price, query, created FROM saved_filters WHERE user_id = ? ORDER BY created DESC", (uid,)).fetchall()
        items = [_row_to_dict(r, ["id", "name", "cat", "city", "max_price", "query", "created"]) for r in rows]
    return {"ok": True, "filters": items}


@app.delete("/saved-filters/{filter_id}")
async def saved_filters_delete(filter_id: int, user: Dict[str, Any] = Depends(get_user)):
    """Удалить сохранённый фильтр."""
    uid = int(user["id"])
    with db_cursor() as conn:
        conn.execute("DELETE FROM saved_filters WHERE id = ? AND user_id = ?", (filter_id, uid))
    return {"ok": True}


# ============================================================
# PROFILE — Мой профиль (всё обо мне)
# ============================================================
@app.get("/profile/me")
async def profile_me(user: Dict[str, Any] = Depends(get_user)):
    """Профиль текущего юзера: мои объявления, баланс, подписки, сделки, реф-стата."""
    uid = int(user["id"])
    listing_cols = ["id", "user_id", "user_name", "user_username", "title", "description", "price", "cat", "type", "contact", "photo", "tier", "city", "status", "created", "expires_at", "channel_message_id", "paid_at"]
    sub_cols = ["id", "query", "cat", "max_price_rub", "city", "active", "is_free", "paid_until", "last_notified"]
    deal_cols = ["id", "listing_id", "amount_rub", "status", "created"]
    with db_cursor() as conn:
        my_active = [_row_to_dict(r, listing_cols) for r in conn.execute(
            "SELECT * FROM listings WHERE user_id = ? AND status = 'active' ORDER BY created DESC LIMIT 50", (uid,)
        ).fetchall()]
        my_total_rows = conn.execute("SELECT COUNT(*) AS n FROM listings WHERE user_id = ? AND status IN ('active','sold')", (uid,)).fetchone()
        bal_rows = conn.execute("SELECT coins, total_earned, vip_until FROM user_balances WHERE user_id = ?", (uid,)).fetchone()
        up_rows = conn.execute("SELECT vip_until FROM user_balances WHERE user_id = ?", (uid,)).fetchone()
        subs = [_row_to_dict(r, sub_cols) for r in conn.execute(
            "SELECT id, query, cat, max_price_rub, city, active, is_free, paid_until, last_notified FROM match_subscriptions WHERE user_id = ? ORDER BY created DESC", (uid,)
        ).fetchall()]
        deals_buyer = [_row_to_dict(r, deal_cols) for r in conn.execute(
            "SELECT id, listing_id, amount_rub, status, created FROM deals WHERE buyer_id = ? ORDER BY created DESC LIMIT 20", (uid,)
        ).fetchall()]
        deals_seller = [_row_to_dict(r, deal_cols) for r in conn.execute(
            "SELECT id, listing_id, amount_rub, status, created FROM deals WHERE seller_id = ? ORDER BY created DESC LIMIT 20", (uid,)
        ).fetchall()]
        fav_count_rows = conn.execute("SELECT COUNT(*) AS n FROM favorites WHERE user_id = ?", (uid,)).fetchone()
        refs_rows = conn.execute("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id = ?", (uid,)).fetchone()
        rating_rows = conn.execute("SELECT AVG(rating)::float AS avg, COUNT(*) AS n FROM reviews WHERE seller_id = ?", (uid,)).fetchone()
        bal = conn.execute("SELECT coins, total_earned FROM user_balances WHERE user_id = ?", (uid,)).fetchone()
        up = conn.execute("SELECT vip_until FROM user_balances WHERE user_id = ?", (uid,)).fetchone()
        my_total_n_row = conn.execute("SELECT COUNT(*) AS n FROM listings WHERE user_id = ? AND status IN ('active','sold')", (uid,)).fetchone()
    now = int(time.time())
    listing_cols = ["id", "user_id", "user_name", "user_username", "title", "description", "price", "cat", "type", "contact", "photo", "tier", "city", "status", "created", "expires_at", "channel_message_id", "paid_at"]
    sub_cols = ["id", "query", "cat", "max_price_rub", "city", "active", "is_free", "paid_until", "last_notified"]
    deal_cols = ["id", "listing_id", "amount_rub", "status", "created"]
    with db_cursor() as conn:
        my_active = [_row_to_dict(r, listing_cols) for r in conn.execute(
            "SELECT * FROM listings WHERE user_id = ? AND status = 'active' ORDER BY created DESC LIMIT 50", (uid,)
        ).fetchall()]
        my_total_rows = conn.execute("SELECT COUNT(*) AS n FROM listings WHERE user_id = ? AND status IN ('active','sold')", (uid,)).fetchone()
        bal_rows = conn.execute("SELECT coins, total_earned, vip_until FROM user_balances WHERE user_id = ?", (uid,)).fetchone()
        up_rows = conn.execute("SELECT vip_until FROM user_balances WHERE user_id = ?", (uid,)).fetchone()
        subs = [_row_to_dict(r, sub_cols) for r in conn.execute(
            "SELECT id, query, cat, max_price_rub, city, active, is_free, paid_until, last_notified FROM match_subscriptions WHERE user_id = ? ORDER BY created DESC", (uid,)
        ).fetchall()]
        deals_buyer = [_row_to_dict(r, deal_cols) for r in conn.execute(
            "SELECT id, listing_id, amount_rub, status, created FROM deals WHERE buyer_id = ? ORDER BY created DESC LIMIT 20", (uid,)
        ).fetchall()]
        deals_seller = [_row_to_dict(r, deal_cols) for r in conn.execute(
            "SELECT id, listing_id, amount_rub, status, created FROM deals WHERE seller_id = ? ORDER BY created DESC LIMIT 20", (uid,)
        ).fetchall()]
        fav_count_rows = conn.execute("SELECT COUNT(*) AS n FROM favorites WHERE user_id = ?", (uid,)).fetchone()
        refs_rows = conn.execute("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id = ?", (uid,)).fetchone()
        rating_rows = conn.execute("SELECT AVG(rating)::float AS avg, COUNT(*) AS n FROM reviews WHERE seller_id = ?", (uid,)).fetchone()
        bal = conn.execute("SELECT coins, total_earned FROM user_balances WHERE user_id = ?", (uid,)).fetchone()
        up = conn.execute("SELECT vip_until FROM user_balances WHERE user_id = ?", (uid,)).fetchone()
        my_total_n_row = conn.execute("SELECT COUNT(*) AS n FROM listings WHERE user_id = ? AND status IN ('active','sold')", (uid,)).fetchone()

    bal_d = {"coins": 0, "total_earned": 0}
    if bal:
        bal_d = {"coins": int(bal["coins"]) if bal.get("coins") else 0, "total_earned": int(bal["total_earned"]) if bal.get("total_earned") else 0}
    try:
        vip_until = int(up["vip_until"]) if up and up.get("vip_until") else 0
    except Exception:
        vip_until = 0
    vip_active = vip_until > now
    my_total_n = int(my_total_n_row["n"]) if my_total_n_row else 0
    fav_n = int(fav_count_rows["n"]) if fav_count_rows else 0
    refs_n = int(refs_rows["n"]) if refs_rows else 0
    rating = {"avg": 0.0, "n": 0}
    if rating_rows:
        rating = {"avg": float(rating_rows["avg"]) if rating_rows.get("avg") else 0.0, "n": int(rating_rows["n"]) if rating_rows.get("n") else 0}
    active_subs = sum(1 for s in subs if s["active"])
    return {
        "ok": True,
        "user_id": uid,
        "user_name": user.get("first_name", ""),
        "user_username": user.get("username", ""),
        "listings_active": len(my_active),
        "listings_pending": len(my_pending),
        "listings_total": my_total_n,
        "my_listings": my_active[:20],
        "my_pending": my_pending[:20],
        "balance": {"coins": bal_d.get("coins", 0), "total_earned": bal_d.get("total_earned", 0)},
        "vip_until": vip_until,
        "vip_active": vip_active,
        "match_subs": {"active": active_subs, "total": len(subs), "items": subs},
        "deals_buyer": deals_buyer,
        "deals_seller": deals_seller,
        "favorites_count": fav_n,
        "referrals_count": refs_n,
        "seller_rating": {"avg": round(float(rating["avg"]), 2) if rating.get("avg") else 0.0, "count": int(rating["n"]) if rating.get("n") else 0},
    }


# ============================================================
# DEALS CRON — auto-refund + auto-release
# ============================================================
async def deals_auto_settle():
    """Run periodically: auto-refund stuck escrowed deals, auto-release overdue shipped deals."""
    try:
        now = int(time.time())
        settled = []
        with db_cursor() as conn:
            # 1. escrowed + created > 3 days ago + still no ship → refund
            stuck = conn.execute(
                "SELECT * FROM deals WHERE status='escrowed' AND created < ?",
                (now - DEAL_AUTO_REFUND_DAYS * 86400,),
            ).fetchall()
            for r in stuck:
                d = _deal_row_to_dict(r)
                conn.execute(
                    "UPDATE deals SET status='refunded', closed_at=?, "
                    "dispute_resolution=? WHERE id=?",
                    (now, "AUTO_REFUND: seller didn't ship in 3 days", d["id"]),
                )
                settled.append({"deal_id": d["id"], "action": "auto_refund"})

            # 2. shipped + auto_release_at passed → release
            overdue = conn.execute(
                "SELECT * FROM deals WHERE status='shipped' AND auto_release_at IS NOT NULL "
                "AND auto_release_at < ?",
                (now,),
            ).fetchall()
            for r in overdue:
                d = _deal_row_to_dict(r)
                _deal_settle_release(conn, d)
                conn.execute(
                    "UPDATE deals SET dispute_resolution=? WHERE id=?",
                    ("AUTO_RELEASE: buyer didn't confirm in 5 days", d["id"]),
                )
                settled.append({"deal_id": d["id"], "action": "auto_release"})

            if settled:
                conn.commit()
        if settled:
            logging.info(f"deals_auto_settle: {settled}")
            # Notify bot parties
            for s in settled:
                row = None
                with db_cursor() as conn:
                    row = conn.execute("SELECT * FROM deals WHERE id=?", (s["deal_id"],)).fetchone()
                if not row:
                    continue
                d = _deal_row_to_dict(row)
                if bot is not None:
                    try:
                        if s["action"] == "auto_refund":
                            for uid in (d["buyer_id"], d["seller_id"]):
                                asyncio.create_task(bot.send_message(int(uid),
                                    f"↩️ Сделка #{s['deal_id']} авто-возврат: продавец не отправил за 3 дня."))
                        else:
                            for uid in (d["buyer_id"], d["seller_id"]):
                                asyncio.create_task(bot.send_message(int(uid),
                                    f"✅ Сделка #{s['deal_id']} авто-подтверждена через 5 дней после отправки."))
                    except Exception as e:
                        logging.warning(f"auto_settle notify failed: {e}")
    except Exception as e:
        logging.error(f"deals_auto_settle error: {e}")


# ============================================================
# Background scheduler: deals_auto_settle + keepalive
# ============================================================
async def _background_scheduler():
    """Run periodic tasks while the app is alive."""
    import asyncio
    while True:
        try:
            await asyncio.sleep(6 * 3600)  # every 6 hours
            await deals_auto_settle()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"background_scheduler error: {e}")


@app.on_event("startup")
async def _start_background():
    import asyncio
    asyncio.create_task(_background_scheduler())


@app.get("/admin/ads")
async def admin_ads(request: Request, x_admin_token: str = Header(None, alias="x-admin-token")):
    """Admin: list all ad creatives + view stats."""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")
    with db_cursor() as conn:
        ads = conn.execute(
            "SELECT id, title, description, image_url, click_url, reward_coins, duration_sec, enabled, weight, "
            "shown_count, click_count, created FROM ad_creatives ORDER BY id"
        ).fetchall()
        stats = conn.execute(
            "SELECT COUNT(*) AS total_views, COALESCE(SUM(coins_credited),0) AS coins_paid, "
            "COUNT(DISTINCT user_id) AS unique_users FROM ad_views"
        ).fetchone()
        bal_totals = conn.execute(
            "SELECT COALESCE(SUM(coins),0) AS outstanding, COALESCE(SUM(total_earned),0) AS all_earned, "
            "COALESCE(SUM(total_spent),0) AS all_spent FROM user_balances"
        ).fetchone()
    def _g(row, key, idx):
        return row.get(key) if isinstance(row, dict) else row[idx]
    return {
        "ok": True,
        "ads": [
            {
                "id": _g(a, "id", 0), "title": _g(a, "title", 1), "description": _g(a, "description", 2),
                "image_url": _g(a, "image_url", 3), "click_url": _g(a, "click_url", 4),
                "reward_coins": _g(a, "reward_coins", 5), "duration_sec": _g(a, "duration_sec", 6),
                "enabled": bool(_g(a, "enabled", 7)), "weight": _g(a, "weight", 8),
                "shown_count": _g(a, "shown_count", 9), "click_count": _g(a, "click_count", 10),
                "created": _g(a, "created", 11),
            } for a in ads
        ],
        "stats": {
            "total_views": _g(stats, "total_views", 0) if stats else 0,
            "coins_paid": _g(stats, "coins_paid", 1) if stats else 0,
            "unique_users": _g(stats, "unique_users", 2) if stats else 0,
        },
        "balances": {
            "outstanding": _g(bal_totals, "outstanding", 0) if bal_totals else 0,
            "total_earned": _g(bal_totals, "all_earned", 1) if bal_totals else 0,
            "total_spent": _g(bal_totals, "all_spent", 2) if bal_totals else 0,
        },
    }


@app.post("/debug/create-vip-test")
async def debug_create_vip_test(request: Request):
    """Debug: create VIP listing for Sasha (real user) for testing invoice flow."""
    if request.headers.get("x-admin-token", "") != ADMIN_TOKEN:
        raise HTTPException(403, "Admin only")
    # Use Sasha's real Telegram ID from header (default)
    user_id = int(request.headers.get("x-telegram-user-id", "748834052"))
    user_name = request.headers.get("x-telegram-user-name", "Sasha")
    user_username = request.headers.get("x-telegram-user-username", "Izdelie0810")
    listing_id = "l_test_" + str(int(datetime.now().timestamp() * 1000))
    with db_cursor() as conn:
        conn.execute(
            "INSERT INTO listings (id, user_id, user_name, user_username, title, description, price, cat, type, contact, photo, tier, city, status, created, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (listing_id, user_id, user_name, user_username, "TEST VIP iPhone 14 Pro", "Тестовое объявление", 75000, "iphone", "sell", "@Izdelie0810", "", "vip", "Москва", "awaiting_payment", int(datetime.now().timestamp()), int(datetime.now().timestamp()) + 7*86400),
        )
        conn.commit()
    # Now try to send invoice
    try:
        import urllib.request, urllib.parse
        amount = TIER_PRICES["vip"]
        tier_name = "VIP 7 дней"
        text = (
            f"👑 iPhone · Продам\n\n"
            f"<b>TEST VIP iPhone 14 Pro</b>\n"
            f"💰 Цена: 75 000 ₽\n\n"
            f"📍 Москва\n"
            f"🔗 https://ibaraholka.p.spru.io/"
        )
        payload = {
            "chat_id": str(user_id),
            "title": f"VIP 7 дней · TEST VIP iPhone 14 Pro",
            "description": text,
            "payload": json.dumps({"listing_id": listing_id, "tier": "vip"}),
            "provider_token": "",
            "currency": "XTR",
            "prices": json.dumps([{"label": tier_name, "amount": amount}]),
        }
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendInvoice",
            data=data,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
        return {
            "listing_id": listing_id,
            "user_id": user_id,
            "invoice_result": result,
        }
    except Exception as e:
        return {"listing_id": listing_id, "error": str(e)}


@app.delete("/listings/{listing_id}")
async def delete_listing(listing_id: str, request: Request):
    """Delete your own listing, or any listing if admin token provided.

    Also removes the corresponding message from the channel.
    """
    admin_token = request.headers.get("x-admin-token", "")
    deleted_from_channel = False
    if admin_token == ADMIN_TOKEN:
        # Admin bypass: delete any listing + from channel
        with db_cursor() as conn:
            row = conn.execute(
                "SELECT channel_message_id FROM listings WHERE id=?",
                (listing_id,)
            ).fetchone()
            ch_msg_id = row["channel_message_id"] if row else None
            conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
            conn.commit()
        deleted_from_channel = await delete_from_channel(ch_msg_id)
        return {"ok": True, "admin": True, "channel_deleted": deleted_from_channel}

    user = await get_user(request.headers.get("authorization", ""))
    user_id_int = int(user["id"])
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT user_id, channel_message_id FROM listings WHERE id=?",
            (listing_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        if int(row["user_id"]) != user_id_int:
            raise HTTPException(403, "Not your listing")
        ch_msg_id = row["channel_message_id"]
        conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
        conn.commit()
    deleted_from_channel = await delete_from_channel(ch_msg_id)
    return {"ok": True, "channel_deleted": deleted_from_channel}


@app.post("/admin/wipe-all-listings")
async def admin_wipe_all_listings(request: Request):
    """One-shot listing cleanup. Accepts admin-token OR wipe-secret derived from BOT_TOKEN.

    Always returns JSON. Use ONLY when user explicitly asks to clear the channel/feed.
    """
    try:
        admin_token_hdr = request.headers.get("x-admin-token", "")
        wipe_secret_hdr = request.headers.get("x-wipe-secret", "")
        expected_secret = __import__("hashlib").sha256(
            ("wipe:" + BOT_TOKEN).encode()
        ).hexdigest()[:24] if BOT_TOKEN else ""

        # Allow if admin-token matches OR wipe-secret matches
        if admin_token_hdr and ADMIN_TOKEN and admin_token_hdr == ADMIN_TOKEN:
            authorized = True
        elif wipe_secret_hdr and wipe_secret_hdr == expected_secret:
            authorized = True
        else:
            raise HTTPException(403, "wipe not authorized")

        with db_cursor() as conn:
            rows = conn.execute(
                "SELECT id, channel_message_id FROM listings"
            ).fetchall()
            count = len(rows)
            ids = [r["id"] for r in rows]
            ch_msg_ids = [r["channel_message_id"] for r in rows if r["channel_message_id"]]
            conn.execute("DELETE FROM listings")
            conn.execute("DELETE FROM ton_payments")
            conn.commit()

        # Try to delete from channel (best effort)
        deleted_ch = []
        for chid in ch_msg_ids:
            try:
                ok = await delete_from_channel(chid)
                if ok:
                    deleted_ch.append(chid)
            except Exception:
                pass

        return {"ok": True, "deleted_listings": count, "deleted_channel_msgs": len(deleted_ch), "ids": ids}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/listings/{listing_id}/confirm-paid")
async def confirm_paid_http(listing_id: str, request: Request):
    """HTTP counterpart of the Telegram `confirm_paid:` callback.

    Lets a user confirm they paid via ЮMoney / ЮKassa / Tinkoff / Sber / etc.
    without leaving the WebApp. The OAuth / Telegram initData header carries
    the user's identity; the listing must belong to that user.

    Two-step flow: marks listing as `paid` (NOT active yet). User must then
    press the "Активировать объявление" button in Mini App (POST /payments/activate)
    for the listing to actually publish to the channel.
    """
    user = await get_user(request.headers.get("authorization", ""))
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        # Ownership check (demo/admin bypass allowed)
        if int(row["user_id"]) not in (999999, int(user["id"])) and int(user["id"]) not in ADMIN_IDS:
            raise HTTPException(403, "Not your listing")
        if row["status"] == "active":
            return {
                "ok": True,
                "listing_id": listing_id,
                "tier": row["tier"],
                "status": "active",
                "already_active": True,
            }
        if row["status"] not in ("awaiting_payment", "paid"):
            raise HTTPException(400, f"bad_status:{row['status']}")
        # Mark as paid; user must then call /payments/activate to publish.
        conn.execute(
            "UPDATE listings SET status='paid', paid_at=extract(epoch from now())::bigint WHERE id=?",
            (listing_id,),
        )
        conn.commit()

    # Notify user (Mini App will see status='paid' via /status endpoint and show Activate button)
    try:
        if bot is not None and row["user_id"] and row["user_id"] != 999999:
            tier_name = "TOP 24 часа" if row["tier"] == "premium" else (
                "VIP 7 дней" if row["tier"] == "vip" else row["tier"].upper()
            )
            await bot.send_message(
                row["user_id"],
                f"✅ <b>Оплата зафиксирована!</b>\n\n"
                f"Объявление <code>{listing_id}</code> ({tier_name}) готово к публикации.\n\n"
                f"Откройте Mini App и нажмите «Активировать объявление».",
            )
    except Exception as e:
        logger.warning(f"confirm_paid_http notify error: {e}")

    return {
        "ok": True,
        "listing_id": listing_id,
        "tier": row["tier"],
        "status": "paid",
        "instruction": "Оплата зафиксирована. Нажмите «Активировать объявление» в Mini App.",
    }


@app.post("/listings/{listing_id}/cancel-payment")
async def cancel_payment_http(listing_id: str, request: Request):
    """User cancelled the payment (closed WebApp without paying).

    Downgrades a paid-tier listing to free tier and removes it from the channel
    if it somehow ended up there (defensive). Free listings get posted to channel
    if they weren't already.
    """
    user = await get_user(request.headers.get("authorization", ""))
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        if int(row["user_id"]) not in (999999, int(user["id"])):
            raise HTTPException(403, "Not your listing")
        # Downgrade to free + clear any channel post
        old_msg = row["channel_message_id"]
        conn.execute(
            "UPDATE listings SET tier='free', status='active', channel_message_id=NULL WHERE id=?",
            (listing_id,),
        )
        conn.commit()
        item_dict = {
            "title": row["title"], "description": row["description"],
            "price": row["price"], "cat": row["cat"], "type": row["type"],
            "contact": row["contact"], "photo": row["photo"],
            "tier": "free", "city": row["city"],
        }

    # Clean up any stale paid-tier channel post
    if old_msg:
        await delete_from_channel(old_msg)

    # Post to channel as free
    posted_msg_id = None
    try:
        from main import ListingIn  # type: ignore
        item = ListingIn(**item_dict)
        user_dict = {
            "id": row["user_id"], "first_name": row["user_name"],
            "username": row["user_username"],
        }
        posted_msg_id = await post_to_channel(listing_id, item, user_dict)
    except Exception as e:
        logger.error(f"cancel_payment_http post_to_channel error: {e}")

    return {
        "ok": True,
        "listing_id": listing_id,
        "new_tier": "free",
        "deleted_old_post": old_msg is not None,
        "new_post_id": posted_msg_id,
    }


@app.get("/admin/listings")
async def admin_listings(admin_token: str = ""):
    """Admin: list all listings. Pass ?admin_token=demo."""
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT id, user_id, user_name, user_username, title, description, price, cat, type, "
            "contact, photo, tier, city, status, created, expires_at, channel_message_id "
            "FROM listings ORDER BY created DESC"
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/admin/stats")
async def admin_stats(admin_token: str = ""):
    """Admin dashboard: revenue, listings by tier/day, top sellers."""
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")

    # Prices in rubles per tier
    PRICES = {"vip": 210, "premium": 70, "free": 0}
    now = int(time.time())
    today_start = now - (now % 86400)  # midnight UTC
    week_start = today_start - 7 * 86400
    month_start = today_start - 30 * 86400

    with db_cursor() as conn:
        # All listings
        all_rows = conn.execute(
            "SELECT id, user_id, user_username, tier, status, created, price FROM listings"
        ).fetchall()

        total = len(all_rows)
        by_tier = {"vip": 0, "premium": 0, "free": 0}
        by_status = {"active": 0, "deleted": 0, "expired": 0}
        revenue = {"today": 0, "week": 0, "month": 0, "all": 0, "today_stars": 0, "week_stars": 0, "month_stars": 0}
        by_day = {}  # date -> revenue in rubles
        by_user = {}  # user_id -> {username, count, revenue}

        for r in all_rows:
            tier = r["tier"] or "free"
            status = r["status"] or "active"
            ts = r["created"] or 0
            price = PRICES.get(tier, 0)

            by_tier[tier] = by_tier.get(tier, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1

            if status == "active" and price > 0:
                # Revenue
                revenue["all"] += price
                if ts >= today_start:
                    revenue["today"] += price
                if ts >= week_start:
                    revenue["week"] += price
                if ts >= month_start:
                    revenue["month"] += price

                # Stars equivalent (XTR ≈ ₽1.4)
                revenue["today_stars"] = revenue["today"] // 1.4
                revenue["week_stars"] = revenue["week"] // 1.4
                revenue["month_stars"] = revenue["month"] // 1.4

                # By day
                day = time.strftime("%Y-%m-%d", time.gmtime(ts))
                by_day[day] = by_day.get(day, 0) + price

                # By user
                uid = r["user_id"]
                if uid:
                    if uid not in by_user:
                        by_user[uid] = {"username": r["user_username"] or "", "count": 0, "revenue": 0}
                    by_user[uid]["count"] += 1
                    by_user[uid]["revenue"] += price

        # Sort top sellers
        top_sellers = sorted(
            [{"user_id": uid, **data} for uid, data in by_user.items()],
            key=lambda x: x["revenue"], reverse=True
        )[:5]

        # Last 7 days chart data (sorted)
        chart_days = []
        for i in range(6, -1, -1):
            d = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
            chart_days.append({"date": d, "revenue": by_day.get(d, 0)})

        # Conversion: listings / paid listings
        paid = by_tier.get("vip", 0) + by_tier.get("premium", 0)
        conversion = (paid / total * 100) if total else 0

        return {
            "total_listings": total,
            "by_tier": by_tier,
            "by_status": by_status,
            "revenue": revenue,
            "conversion_pct": round(conversion, 1),
            "top_sellers": top_sellers,
            "chart": chart_days,
            "generated_at": now,
        }


@app.get("/admin/autopost")
async def admin_autopost(request: Request, listing_id: str = "", tier: str = ""):
    """Autopost: pick the next active listing and publish it to channel.

    Called by cron 'Автопост 10:00' and any scheduled promo.

    Strategy:
      1) If listing_id provided in query — use it.
      2) Else if tier provided — pick random active listing of that tier.
      3) Else — pick oldest active listing that has no channel_message_id yet
         (rotation: never-published first, then by created asc).

    Auth: query param ?token=<ADMIN_TOKEN>.
    Returns: {ok, posted, listing_id, message_id, reason}.
    """
    auth = (
        request.headers.get("x-admin-token", "")
        or request.query_params.get("token", "")
    )
    if auth != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")

    with db_cursor() as conn:
        if listing_id:
            row = conn.execute(
                "SELECT * FROM listings WHERE id=?", (listing_id,)
            ).fetchone()
        elif tier:
            row = conn.execute(
                "SELECT * FROM listings WHERE status='active' AND tier=? "
                "ORDER BY created ASC LIMIT 1",
                (tier,),
            ).fetchone()
        else:
            # First: active listings never posted to channel (free tier preferred).
            row = conn.execute(
                "SELECT * FROM listings WHERE status='active' "
                "AND channel_message_id IS NULL "
                "AND tier IN ('free','premium','vip') "
                "ORDER BY created ASC LIMIT 1"
            ).fetchone()
            if not row:
                # Fallback: oldest active listing (rotation).
                row = conn.execute(
                    "SELECT * FROM listings WHERE status='active' "
                    "ORDER BY created ASC LIMIT 1"
                ).fetchone()

    if not row:
        return {"ok": False, "posted": False, "reason": "no active listings"}

    item_id = row["id"]
    class _L: pass
    item = _L()
    item.title = row["title"]
    item.description = row["description"]
    item.price = row["price"]
    item.cat = row["cat"]
    item.type = row["type"]
    item.contact = row["contact"]
    item.photo = row["photo"] or ""
    item.tier = row["tier"]
    item.city = row["city"]
    user = {
        "first_name": row["user_name"] or "Продавец",
        "username": row["user_username"],
        "id": row["user_id"],
    }

    try:
        await post_to_channel(item_id, item, user)
        return {
            "ok": True,
            "posted": True,
            "listing_id": item_id,
            "tier": row["tier"],
            "title": row["title"],
            "price": row["price"],
        }
    except Exception as e:
        logger.error(f"admin_autopost failed: {e}")
        return {"ok": False, "posted": False, "listing_id": item_id, "error": str(e)}


@app.post("/admin/post-channel")
async def admin_post_channel(request: Request):
    """Admin: post a listing to channel manually."""
    admin_token = request.headers.get("x-admin-token", "")
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")
    body = await request.json()
    listing_id = body.get("listing_id", "")
    if not listing_id:
        raise HTTPException(400, "listing_id required")

    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Listing not found")

    # Build ListingIn-like dict
    class _L:
        pass
    item = _L()
    item.title = row["title"]
    item.description = row["description"]
    item.price = row["price"]
    item.cat = row["cat"]
    item.type = row["type"]
    item.contact = row["contact"]
    item.photo = row["photo"] or ""
    item.tier = row["tier"]
    item.city = row["city"]
    user = {"first_name": row["user_name"] or "Продавец", "username": row["user_username"], "id": row["user_id"]}

    try:
        await post_to_channel(listing_id, item, user)
        return {"ok": True, "posted": True}
    except Exception as e:
        logger.error(f"admin_post_channel failed: {e}")
        return {"ok": False, "error": str(e)}


@app.post("/admin/purge-unpaid-channel-posts")
async def purge_unpaid_channel_posts(request: Request):
    """Admin: delete channel posts for paid-tier listings whose payment never completed.

    Scans the database for premium/vip listings that have a channel_message_id
    but status != 'active', removes those messages from @ibaraholkatyt, and
    clears channel_message_id. Idempotent — safe to run again.
    """
    admin_token = request.headers.get("x-admin-token", "")
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")

    purged = []
    failed = []
    with db_cursor() as conn:
        rows = conn.execute(
            """SELECT id, tier, status, channel_message_id
               FROM listings
               WHERE tier IN ('premium', 'vip')
                 AND status != 'active'
                 AND channel_message_id IS NOT NULL"""
        ).fetchall()

    for row in rows:
        ok = await delete_from_channel(row["channel_message_id"])
        if ok:
            with db_cursor() as conn:
                conn.execute(
                    "UPDATE listings SET channel_message_id=NULL WHERE id=?",
                    (row["id"],),
                )
                conn.commit()
            purged.append({"id": row["id"], "tier": row["tier"], "status": row["status"]})
        else:
            failed.append({"id": row["id"], "msg_id": row["channel_message_id"]})

    logger.info(
        f"purge_unpaid_channel_posts: purged={len(purged)} failed={len(failed)}"
    )
    return {"ok": True, "purged": purged, "failed": failed}


@app.post("/admin/recheck-channel-post")
async def recheck_channel_post(request: Request):
    """Admin: verify a listing's current payment gate result without reposting."""
    admin_token = request.headers.get("x-admin-token", "")
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(403, "Admin token required")
    body = await request.json()
    listing_id = body.get("listing_id", "")
    if not listing_id:
        raise HTTPException(400, "listing_id required")
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT id, tier, status, channel_message_id FROM listings WHERE id=?",
            (listing_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Listing not found")
    is_paid = (row["tier"] or "free") in ("premium", "vip")
    would_post = (not is_paid) or (row["status"] == "active")
    return {
        "id": row["id"],
        "tier": row["tier"],
        "status": row["status"],
        "has_channel_post": row["channel_message_id"] is not None,
        "channel_message_id": row["channel_message_id"],
        "would_post_to_channel": would_post,
    }


@app.post("/admin/listings/{listing_id}/approve")
async def approve_listing(listing_id: str, request: Request):
    """Admin: approve a pending listing. Requires ADMIN_IDS set."""
    if not ADMIN_IDS:
        raise HTTPException(403, "Admin not configured")
    user = await get_user(request.headers.get("authorization", ""))
    if user["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Admin only")
    with db_cursor() as conn:
        conn.execute("UPDATE listings SET status='active' WHERE id=?", (listing_id,))
        conn.commit()
    return {"ok": True}


@app.post("/admin/listings/{listing_id}/reject")
async def reject_listing(listing_id: str, request: Request):
    """Admin: reject an auto-activated listing (e.g. Tinkoff payment didn't arrive).

    Sets status back to deleted and removes the channel message if any.
    """
    if not ADMIN_IDS:
        raise HTTPException(403, "Admin not configured")
    user = await get_user(request.headers.get("authorization", ""))
    if user["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Admin only")

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    reason = body.get("reason", "admin_reject")

    ch_msg_id = None
    with db_cursor() as conn:
        row = conn.execute("SELECT channel_message_id FROM listings WHERE id=?", (listing_id,)).fetchone()
        if row:
            ch_msg_id = row["channel_message_id"]
        conn.execute("UPDATE listings SET status='deleted' WHERE id=?", (listing_id,))
        conn.commit()

    if ch_msg_id:
        try:
            await delete_from_channel(ch_msg_id)
        except Exception:
            pass

    return {"ok": True, "listing_id": listing_id, "reason": reason, "channel_deleted": bool(ch_msg_id)}


# ============================================================
# Run: bot (polling) + API (uvicorn) in same process
# ============================================================
async def run_bot():
    """Run aiogram bot in polling mode."""
    if bot is None or dp is None:
        logger.warning("⚠️  Bot not initialized (no BOT_TOKEN) — skipping polling. API will still run.")
        # Keep task alive forever
        while True:
            await asyncio.sleep(3600)
        return
    logger.info("🤖 Starting bot polling (skip — using webhook)...")
    # We use webhook endpoint at /webhook/telegram instead of long polling
    # because polling is unreliable on free Render (instance sleeps).
    # Webhook handler is defined above in telegram_webhook() route.
    logger.info("🤖 Bot ready — Telegram will POST updates to /webhook/telegram")
    # Keep task alive (uvicorn handles webhook requests)
    while True:
        await asyncio.sleep(3600)


async def run_api():
    """Run FastAPI via uvicorn."""
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        log_level="info",
    )
    server = uvicorn.Server(config)
    logger.info(f"🌐 Starting API on port {config.port}")
    await server.serve()


async def main():
    try:
        init_db()
    except Exception as e:
        logger.error(f"init_db failed: {e}", flush=True)
    # Run bot and API concurrently
    await asyncio.gather(run_bot(), run_api())



@app.post("/debug/migrate")
def run_migration(source_db: str = "ibaraholka.db", body: dict = None):
    """One-shot: copy rows from local sqlite to postgres.

    Looks for sqlite file in:
    1. CWD / given path
    2. /tmp/
    3. /data/
    4. Falls back to uploading via JSON body: {"sqlite_b64": "..."}
    """
    import io
    import contextlib
    import base64
    if not USE_POSTGRES:
        return {"ok": False, "error": "Postgres not configured"}
    # Try local paths
    paths_to_try = [source_db, os.path.join("/tmp", source_db), os.path.join("/data", source_db)]
    found = None
    for p in paths_to_try:
        if os.path.exists(p):
            found = p
            break
    # Fallback: inline upload via body
    if not found and body and body.get("sqlite_b64"):
        try:
            raw = base64.b64decode(body["sqlite_b64"])
            with open("/tmp/ibaraholka.db", "wb") as f:
                f.write(raw)
            found = "/tmp/ibaraholka.db"
        except Exception as e:
            return {"ok": False, "error": f"decode failed: {e}"}
    if not found:
        return {"ok": False, "error": f"sqlite not found in: {paths_to_try} (no body either)"}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            migrate_sqlite_to_pg(found)
        return {"ok": True, "log": buf.getvalue()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500], "log": buf.getvalue()}


@app.get("/debug/test-pg")
def test_postgres():
    """Test direct PostgreSQL connection."""
    import os
    import psycopg2
    url = os.getenv("DATABASE_URL", "").strip()
    # Debug: report length and prefix so we can verify Render stored it correctly
    import hashlib
    dbg = {
        "url_len": len(url),
        "url_hash": hashlib.sha256(url.encode()).hexdigest()[:16] if url else None,
        "url_prefix": url[:35] + "..." if len(url) > 35 else url,
    }
    if not url:
        return {"ok": False, "error": "DATABASE_URL not set", "debug": dbg}
    try:
        with psycopg2.connect(url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), version();")
                db_name, version = cur.fetchone()
                cur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema='public' ORDER BY table_name
                """)
                tables = [r[0] for r in cur.fetchall()]
                # Also test the listings query through db_cursor
                test = []
                try:
                    with db_cursor() as dconn:
                        cur2 = dconn.execute("SELECT id, title, price, tier FROM listings WHERE status='active' LIMIT 3")
                        for row in cur2.fetchall():
                            try:
                                test.append(dict(row))
                            except Exception as e:
                                test.append({"_err": str(e), "_raw": str(row)})
                except Exception as e:
                    test = [{"_query_err": str(e)}]
                return {
                    "ok": True,
                    "database": db_name,
                    "version": version[:60],
                    "tables": tables,
                    "tables_count": len(tables),
                    "sample_listings": test,
                    "debug": dbg,
                }
    except Exception as e:
        return {"ok": False, "error": str(e)[:300], "debug": dbg}


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped")
    except Exception as e:
        logger.error(f"Fatal: {e}", flush=True)
        # Keep process alive so Railway doesn't restart-loop
        import time
        while True:
            time.sleep(60)


# ============================================================
# Telegram webhook
# ============================================================
WEBHOOK_PATH = "/webhook/telegram"

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    """Receive updates from Telegram webhook instead of polling."""
    if not dp:
        raise HTTPException(503, "Bot not initialized")
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(400, f"Bad JSON: {e}")

    # Feed update to dispatcher as a fake message via aiogram Bot method
    try:
        from aiogram import types as aiogram_types
        update = aiogram_types.Update(**body)
        # Process synchronously (handle them all in one request)
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook handler error: {e}", flush=True)
        return {"ok": False, "error": str(e)}


@app.post("/debug/setup-webhook")
async def setup_webhook(request: Request):
    """
    One-time setup: register Telegram webhook URL with Telegram Bot API.
    Body: {"url": "https://ibaraholka-bot.onrender.com/webhook/telegram"}
    Or auto-detect from request host.
    """
    if not bot:
        raise HTTPException(503, "Bot not initialized")
    try:
        body = await request.json() if (await request.body()) else {}
    except Exception:
        body = {}
    
    provided_url = body.get("url")
    if provided_url:
        webhook_url = f"{provided_url.rstrip('/')}{WEBHOOK_PATH}"
    else:
        # Auto from request host
        host = request.headers.get("host", "ibaraholka-bot.onrender.com")
        scheme = request.headers.get("x-forwarded-proto", "https")
        webhook_url = f"{scheme}://{host}{WEBHOOK_PATH}"
    
    # Set webhook
    result = await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    info = await bot.get_webhook_info()
    return {
        "ok": True,
        "webhook_url": webhook_url,
        "set_webhook_result": result,
        "webhook_info": {
            "url": info.url,
            "pending_update_count": info.pending_update_count,
            "last_error_message": info.last_error_message,
            "last_error_date": info.last_error_date,
            "max_connections": info.max_connections,
        }
    }

f# deploy-trigger 1789698345 Саша сделал Manual Deploy но Render скачал СТАРУЮ версию
