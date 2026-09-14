"""
АйБарахолка · Telegram-бот + FastAPI бэкенд
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
import sqlite3
import json
import hmac
import hashlib
import urllib.parse
from datetime import datetime
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# ============================================================
# Config
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://ibaraholka.p.spru.io/").strip()
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ibaraholkatyt").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "❌ Set BOT_TOKEN env var.\n"
        "   1) Открой @BotFather в Telegram\n"
        "   2) /newbot → следуй инструкциям\n"
        "   3) Скопируй токен в BOT_TOKEN"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ibaraholka")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

DB_FILE = "ibaraholka.db"


# ============================================================
# Database
# ============================================================
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
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
            expires_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_status ON listings(status);
        CREATE INDEX IF NOT EXISTS idx_tier ON listings(tier);
        CREATE INDEX IF NOT EXISTS idx_cat ON listings(cat);
        CREATE INDEX IF NOT EXISTS idx_user ON listings(user_id);
        CREATE INDEX IF NOT EXISTS idx_created ON listings(created);
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
    """Get Telegram user from Authorization: tma <initData>."""
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Authorization header required: 'tma <initData>'")
    return validate_init_data(authorization[4:])


# ============================================================
# Models
# ============================================================
TIER_PRICES = {"premium": 50, "vip": 150}  # Stars
TIER_DURATIONS = {"premium": 24 * 3600, "vip": 7 * 24 * 3600}  # seconds
TIER_LABELS = {"free": "Бесплатно", "premium": "⭐ TOP 24ч (50⭐)", "vip": "👑 VIP 7 дней (150⭐)"}


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


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS and ADMIN_IDS:
        return
    with get_db() as conn:
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


@dp.message(F.successful_payment)
async def success_payment(message: types.Message):
    payload = json.loads(message.successful_payment.invoice_payload or "{}")
    listing_id = payload.get("listing_id")
    tier = payload.get("tier", "")

    if listing_id:
        with get_db() as conn:
            conn.execute("UPDATE listings SET status='active' WHERE id=?", (listing_id,))
            conn.commit()
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


async def post_to_channel(listing_id: str, item: ListingIn, user: Dict[str, Any]):
    """Post a listing to the configured channel."""
    try:
        text, keyboard = format_listing_for_channel(item, user)
        if item.photo and item.photo.startswith("data:image"):
            # base64 photo — try to send as photo (Telegram accepts URLs but not data URLs)
            # For now we send text-only; photos via base64 are larger than 5MB which is the photo limit
            logger.info(f"Listing {listing_id} has base64 photo, sending text-only to channel")
            await bot.send_message(CHANNEL_ID, text, reply_markup=keyboard, disable_web_page_preview=True)
        else:
            await bot.send_message(CHANNEL_ID, text, reply_markup=keyboard, disable_web_page_preview=True)
        logger.info(f"Posted listing {listing_id} to channel {CHANNEL_ID}")
    except Exception as e:
        logger.error(f"Failed to post listing {listing_id} to channel: {e}")


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


@app.get("/")
def root():
    return {"app": "АйБарахолка API", "version": "1.0.0", "status": "ok"}


@app.get("/health")
def health():
    return {"ok": True, "ts": int(datetime.now().timestamp())}


@app.get("/listings")
def list_listings(
    cat: Optional[str] = Query(None, pattern="^(iphone|airpods|ipad|mac|watch|accs)$"),
    type: Optional[str] = Query(None, pattern="^(sell|buy|exchange|opt)$"),
    limit: int = Query(100, ge=1, le=500),
):
    """Public list of active listings (sorted by tier then recency)."""
    now = int(datetime.now().timestamp())
    with get_db() as conn:
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
        q += (
            " ORDER BY CASE tier WHEN 'vip' THEN 0 WHEN 'premium' THEN 1 ELSE 2 END, "
            "created DESC LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/listings")
async def create_listing(item: ListingIn, request: Request):
    """Create new listing. Requires Telegram WebApp Authorization."""
    user = await get_user(request.headers.get("authorization", ""))

    listing_id = "l_" + str(int(datetime.now().timestamp() * 1000))

    # Tier expiry
    expires_at = None
    if item.tier in TIER_DURATIONS:
        expires_at = int(datetime.now().timestamp()) + TIER_DURATIONS[item.tier]

    initial_status = "active" if item.tier == "free" else "awaiting_payment"

    with get_db() as conn:
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
        f"status={initial_status}"
    )

    # Post to channel if active
    if item.tier in ("premium", "vip"):
        await post_to_channel(listing_id, item, user)

    # Paid tier → send Stars invoice
    invoice_url = None
    if item.tier in ("premium", "vip"):
        amount = TIER_PRICES[item.tier]
        tier_name = "TOP 24 часа" if item.tier == "premium" else "VIP 7 дней"
        try:
            sent_message = await bot.send_invoice(
                chat_id=user["id"],
                title=f"АйБарахолка · {tier_name}",
                description=(
                    f"Платное размещение для:\n<i>{item.title[:80]}</i>\n\n"
                    f"Наверху ленты — {('24 часа' if item.tier == 'premium' else '7 дней')}."
                ),
                payload=json.dumps({"listing_id": listing_id, "tier": item.tier}),
                provider_token="",  # empty for Stars
                currency="XTR",       # Telegram Stars
                prices=[LabeledPrice(label=tier_name, amount=amount)],
            )
            invoice_msg_id = sent_message.message_id
        except Exception as e:
            logger.error(f"Invoice send failed: {e}")
            raise HTTPException(500, f"Не удалось отправить инвойс: {e}")
    else:
        invoice_msg_id = None

    return {
        "id": listing_id,
        "status": initial_status,
        "tier": item.tier,
        "invoice_sent": invoice_msg_id is not None,
    }


@app.delete("/listings/{listing_id}")
async def delete_listing(listing_id: str, request: Request):
    """Delete your own listing."""
    user = await get_user(request.headers.get("authorization", ""))
    with get_db() as conn:
        row = conn.execute("SELECT user_id FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        if row["user_id"] != user["id"]:
            raise HTTPException(403, "Not your listing")
        conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
        conn.commit()
    return {"ok": True}


@app.post("/admin/listings/{listing_id}/approve")
async def approve_listing(listing_id: str, request: Request):
    """Admin: approve a pending listing. Requires ADMIN_IDS set."""
    if not ADMIN_IDS:
        raise HTTPException(403, "Admin not configured")
    user = await get_user(request.headers.get("authorization", ""))
    if user["id"] not in ADMIN_IDS:
        raise HTTPException(403, "Admin only")
    with get_db() as conn:
        conn.execute("UPDATE listings SET status='active' WHERE id=?", (listing_id,))
        conn.commit()
    return {"ok": True}


# ============================================================
# Run: bot (polling) + API (uvicorn) in same process
# ============================================================
async def run_bot():
    """Run aiogram bot in polling mode."""
    logger.info("🤖 Starting bot polling...")
    # Delete webhook (if any) to use polling
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, handle_signals=False)


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
    init_db()
    # Run bot and API concurrently
    await asyncio.gather(run_bot(), run_api())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped")
