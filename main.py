"""
АйБарахолка · Telegram-бот + FastAPI бэкенд
============================================

Build: 2026-09-14T18:30 force-redeploy-test
============================================
- aiogram 3.x для бота (polling режим)
- FastAPI для HTTP API, который вызывает Telegram Mini App
- SQLite для хранения объявлений
- Telegram Stars (XTR) для оплаты платных размещений

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
import httpx
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request, Query, Depends
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
        """)


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
TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "UQPLACEHOLDER_SET_IN_RENDER_ENV").strip()
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

    if item.tier in ("premium", "vip"):
        if is_demo_user:
            skip_invoice_reason = "demo user (DEMO_MODE=1) — no payment required"
        elif not BOT_TOKEN:
            invoice_error = "BOT_TOKEN not set"
        else:
            try:
                amount = TIER_PRICES[item.tier]
                tier_name = "TOP 24 часа" if item.tier == "premium" else "VIP 7 дней"
                import urllib.request
                import urllib.parse
                invoice_payload = {
                    "chat_id": str(user["id"]),
                    "title": f"{tier_name} · {item.title[:40]}",
                    "description": (
                        f"📱 <b>{item.title}</b>\n\n"
                        f"💰 Цена: {item.price:,} ₽\n"
                        f"📍 {item.city}\n\n"
                        f"<b>Что даёт {tier_name}:</b>\n"
                        f"{('• Размещение в топе ленты 24 часа\n• Выделение золотом' if item.tier == 'premium' else '• Размещение в VIP-зоне 7 дней\n• Приоритет в поиске\n• Бейдж VIP')}".
                        rstrip()
                    ),
                    "payload": json.dumps({"listing_id": listing_id, "tier": item.tier}),
                    "provider_token": "",
                    "currency": "XTR",
                    "prices": json.dumps([{"label": tier_name, "amount": amount}]),
                }
                # Add photo if item has one (not base64 — Telegram needs URL or file_id)
                # For now, skip photo in invoice (can be added later with photo upload)
                data = urllib.parse.urlencode(invoice_payload).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendInvoice",
                    data=data,
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read().decode())
                if result.get("ok"):
                    invoice_msg_id = result["result"]["message_id"]
                    log_msg = f"[INVOICE] {listing_id}: sent msg_id={invoice_msg_id}"
                    print(log_msg, flush=True)
                else:
                    invoice_error = str(result)
                    log_msg = f"[INVOICE] {listing_id}: TG error: {result}"
                    print(log_msg, flush=True)
                try:
                    with open("/data/last_post.log", "a") as f:
                        f.write(log_msg + "\n")
                except Exception:
                    pass
            except Exception as e:
                invoice_error = str(e)
                log_msg = f"[INVOICE] {listing_id}: EXCEPTION {type(e).__name__}: {e}"
                print(log_msg, flush=True)
                try:
                    with open("/data/last_post.log", "a") as f:
                        f.write(log_msg + "\n")
                except Exception:
                    pass

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
    user_id = int(body.get("user_id", 0) or 0)
    tier = body.get("tier", "")

    if not listing_id:
        return {"ok": False, "error": "no listing_id"}
    if not user_id:
        return {"ok": False, "error": "no user_id"}

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
        import logging
        logging.info(f"PAYMENT_PAID listing={listing_id} user={user_id} tier={tier} method=Tinkoff")
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
                # Already confirmed; idempotent re-activation.
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

        # Mark confirmed and activate listing.
        now = int(time.time())
        with db_cursor() as conn:
            conn.execute(
                "UPDATE ton_payments SET confirmed=?, tx_hash=?, tx_time=?, user_id=? "
                "WHERE comment=?",
                (now, tx_hash, now, user_id, comment),
            )
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


def _credit_due_ads(conn, user_id: int, now_ms: int) -> int:
    """Auto-credit all 'due' ad views for user (created + duration_sec*1000 <= now_ms).

    Returns total coins credited this call. Used by /user/balance, /ads/next,
    /payments/coins/pay — guarantees coins arrive even if user closed the app.
    """
    try:
        rows = conn.execute(
            "SELECT v.id, v.ad_id, v.coins_credited "
            "FROM ad_views v WHERE v.user_id=? AND v.completed=0 "
            "AND EXISTS (SELECT 1 FROM ad_creatives a WHERE a.id=v.ad_id AND v.created + a.duration_sec*1000 <= ?)",
            (user_id, now_ms),
        ).fetchall()
    except Exception as e:
        logging.warning("_credit_due_ads query failed: %s", e)
        return 0
    if not rows:
        return 0
    total = 0
    credited_ad_ids = []
    for view_id, ad_id, coins_credited in rows:
        total += int(coins_credited or 0)
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
        if bal:
            new_coins = (bal[0] or 0) + total
            new_earned = (bal[1] or 0) + total
            conn.execute(
                "UPDATE user_balances SET coins=?, total_earned=?, updated=? WHERE user_id=?",
                (new_coins, new_earned, now_ms, user_id),
            )
        else:
            conn.execute(
                "INSERT INTO user_balances (user_id, coins, total_earned, total_spent, updated) "
                "VALUES (?, ?, ?, 0, ?)",
                (user_id, total, total, now_ms),
            )
        for ad_id in credited_ad_ids:
            conn.execute(
                "UPDATE ad_creatives SET shown_count = shown_count + 1 WHERE id=?",
                (ad_id,),
            )
    return total


@app.post("/ads/start")
async def ads_start(request: Request, user: Dict = Depends(get_user)):
    """User started watching an ad. Records a PENDING view (completed=0).

    Server auto-credits when duration_sec passes — client doesn't have to
    stay on the page. Just check /user/balance later.
    """
    user_id = int(user["id"])
    now_ms = int(datetime.now().timestamp() * 1000)
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
        if not ad_row or not ad_row[2]:
            return {"ok": False, "error": "ad_disabled"}
        reward, duration_sec, _ = ad_row
        view_id = int(now_ms) ^ user_id
        conn.execute(
            "INSERT INTO ad_views (id, user_id, ad_id, coins_credited, created, completed) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (view_id, user_id, ad_id, reward, now_ms),
        )
        conn.commit()
    return {
        "ok": True,
        "ad_id": ad_id,
        "duration_sec": duration_sec,
        "reward": reward,
        "pending_until_ts": now_ms + duration_sec * 1000,
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
    now_ms = int(datetime.now().timestamp() * 1000)

    with db_cursor() as conn:
        # Auto-credit any ads whose duration has already passed (server-side timer)
        credited_now = _credit_due_ads(conn, user_id, now_ms)
        if credited_now > 0:
            conn.commit()
        # Anti-fraud: последний просмотр
        last = conn.execute(
            "SELECT created FROM ad_views WHERE user_id=? ORDER BY created DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if last and (now_ms - last[0]) < AD_COOLDOWN_SEC * 1000:
            wait_sec = AD_COOLDOWN_SEC - int((now_ms - last[0]) / 1000)
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
        last_ad_id = last_ad_row[0] if last_ad_row else None

        ads = conn.execute(
            "SELECT id, title, description, image_url, click_url, reward_coins, duration_sec "
            "FROM ad_creatives WHERE enabled=1 ORDER BY weight DESC, RANDOM() LIMIT 20"
        ).fetchall()
        if not ads:
            return {"ok": False, "reason": "no_ads", "message": "Нет активной рекламы"}

        # Prefer ads different from last shown
        candidates = [a for a in ads if a[0] != last_ad_id] or ads
        ad = candidates[0]
        ad_id, title, desc, img, click, reward, dur = ad

        return {
            "ok": True,
            "ad": {
                "id": ad_id,
                "title": title,
                "description": desc,
                "image_url": img,
                "click_url": click,
                "reward_coins": reward,
                "duration_sec": dur,
            },
        }


@app.post("/ads/watch-complete")
async def ads_watch_complete(request: Request, user: Dict = Depends(get_user)):
    """User finished watching ad (after duration_sec). Credit IB Coins.

    Body: {ad_id, view_id, duration_sec}
    Server re-checks: cooldown (30s), ad exists & enabled, duration matches.
    """
    user_id = int(user["id"])
    now_ms = int(datetime.now().timestamp() * 1000)

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
        if not ad_row or not ad_row[2]:
            return {"ok": False, "error": "ad_disabled"}
        reward, duration_sec, _ = ad_row

        # Anti-fraud: cooldown check
        last = conn.execute(
            "SELECT created FROM ad_views WHERE user_id=? ORDER BY created DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if last and (now_ms - last[0]) < AD_COOLDOWN_SEC * 1000:
            wait_sec = AD_COOLDOWN_SEC - int((now_ms - last[0]) / 1000)
            return {"ok": False, "error": "cooldown", "wait_sec": max(wait_sec, 1)}

        # Insert view record + update balance (atomic via SQL)
        view_id = int(now_ms) ^ user_id  # simple unique-ish
        conn.execute(
            "INSERT INTO ad_views (id, user_id, ad_id, coins_credited, created, completed) VALUES (?, ?, ?, ?, ?, 1)",
            (view_id, user_id, ad_id, reward, now_ms),
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
            new_coins = bal[0] + reward
            new_earned = bal[1] + reward
            conn.execute(
                "UPDATE user_balances SET coins=?, total_earned=?, updated=? WHERE user_id=?",
                (new_coins, new_earned, now_ms, user_id),
            )
        else:
            new_coins = reward
            new_earned = reward
            conn.execute(
                "INSERT INTO user_balances (user_id, coins, total_earned, total_spent, updated) VALUES (?, ?, ?, 0, ?)",
                (user_id, new_coins, new_earned, now_ms),
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
    now_ms = int(datetime.now().timestamp() * 1000)
    with db_cursor() as conn:
        # Auto-credit any ads whose duration has already passed
        credited_now = _credit_due_ads(conn, user_id, now_ms)
        if credited_now > 0:
            conn.commit()
        bal = conn.execute(
            "SELECT coins, total_earned, total_spent, updated FROM user_balances WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if bal:
        return {
            "ok": True,
            "coins": bal[0],
            "total_earned": bal[1],
            "total_spent": bal[2],
            "updated": bal[3],
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
    now_ms = int(datetime.now().timestamp() * 1000)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    listing_id = body.get("listing_id", "")
    if not listing_id:
        raise HTTPException(400, "listing_id required")

    with db_cursor() as conn:
        # Auto-credit any ads whose duration has already passed
        credited_now = _credit_due_ads(conn, user_id, now_ms)
        if credited_now > 0:
            conn.commit()
        # Lock listing row
        row = conn.execute(
            "SELECT id, user_id, tier, status, title, price FROM listings WHERE id=?",
            (listing_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "listing_not_found"}
        if int(row[1]) != user_id:
            return {"ok": False, "error": "not_owner"}
        tier = row[2]
        status = row[3]
        title = row[4]

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
        coins = bal[0] if bal else 0
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
            (new_coins, price_coins, now_ms, user_id),
        )

        # Activate listing (set expires_at if missing)
        expires_at = int(datetime.now().timestamp()) + TIER_DURATIONS.get(tier, 7 * 86400)
        conn.execute(
            "UPDATE listings SET status='active', paid_at=?, expires_at=? WHERE id=?",
            (now_ms, expires_at, listing_id),
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
    return {
        "ok": True,
        "ads": [
            {
                "id": a[0], "title": a[1], "description": a[2], "image_url": a[3],
                "click_url": a[4], "reward_coins": a[5], "duration_sec": a[6],
                "enabled": bool(a[7]), "weight": a[8], "shown_count": a[9],
                "click_count": a[10], "created": a[11],
            } for a in ads
        ],
        "stats": {
            "total_views": stats[0],
            "coins_paid": stats[1],
            "unique_users": stats[2],
        },
        "balances": {
            "outstanding": bal_totals[0],
            "total_earned": bal_totals[1],
            "total_spent": bal_totals[2],
        },
    }


@app.get("/debug/list-ads")
async def debug_list_ads():
    """Temporary: dump ad_creatives contents via both code paths."""
    import os
    import psycopg2
    url = os.getenv("DATABASE_URL", "").strip()
    out = {"via_db_cursor": None, "via_psycopg2": None}
    try:
        with db_cursor() as conn:
            rows = conn.execute("SELECT id, title, enabled, reward_coins, duration_sec FROM ad_creatives ORDER BY id").fetchall()
            out["via_db_cursor"] = [{"id": r[0], "title": r[1], "enabled": r[2], "reward": r[3], "dur": r[4]} for r in rows]
    except Exception as e:
        out["via_db_cursor"] = {"err": str(e)}
    try:
        with psycopg2.connect(url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, title, enabled, reward_coins, duration_sec FROM ad_creatives ORDER BY id")
                rows = cur.fetchall()
                out["via_psycopg2"] = [{"id": r[0], "title": r[1], "enabled": r[2], "reward": r[3], "dur": r[4]} for r in rows]
    except Exception as e:
        out["via_psycopg2"] = {"err": str(e)}
    return out


@app.get("/debug/run-seed")
async def debug_run_seed():
    """Force-run lazy seed and return result/error."""
    import traceback
    try:
        _seed_ads_if_empty()
        # verify
        with db_cursor() as conn:
            rows = conn.execute("SELECT id, title, enabled FROM ad_creatives ORDER BY id").fetchall()
        def _g(r, k):
            return r[k] if isinstance(r, dict) else r[0]
        out = []
        for r in rows:
            d = r if isinstance(r, dict) else None
            out.append({
                "id": d["id"] if d else r[0],
                "title": d["title"] if d else r[1],
                "enabled": d["enabled"] if d else r[2],
            })
        return {"ok": True, "rows": out}
    except Exception as e:
        return {"ok": False, "err": str(e), "tb": traceback.format_exc()[:1500]}


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
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT user_id, channel_message_id FROM listings WHERE id=?",
            (listing_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        if row["user_id"] != user["id"]:
            raise HTTPException(403, "Not your listing")
        ch_msg_id = row["channel_message_id"]
        conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
        conn.commit()
    deleted_from_channel = await delete_from_channel(ch_msg_id)
    return {"ok": True, "channel_deleted": deleted_from_channel}


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
        if row["user_id"] not in (999999, user["id"]) and user["id"] not in ADMIN_IDS:
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
        if row["user_id"] not in (999999, user["id"]):
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
