import os
import json
import hmac
import hashlib
import sqlite3
import asyncio
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ============ ENV ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")

# Railway provides PORT
PORT = int(os.getenv("PORT", "8000"))

PRICE_CURRENCY = "usd"
PAY_CURRENCY = "usdt"  # We'll receive USDT (your account settlement should be set to USDT TRC20)

if not BOT_TOKEN or not ADMIN_ID or not NOWPAYMENTS_API_KEY or not NOWPAYMENTS_IPN_SECRET:
    raise RuntimeError("Missing environment variables. Check Railway Variables.")

# ============ DB ============
DB_PATH = "orders.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            amount_usd REAL NOT NULL,
            invoice_id TEXT,
            invoice_url TEXT,
            status TEXT DEFAULT 'created'
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            order_id INTEGER,
            status TEXT,
            raw_json TEXT,
            updated_at TEXT
        )
        """)

def create_order(chat_id: int, amount_usd: float) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO orders (created_at, chat_id, amount_usd) VALUES (?, ?, ?)",
            (datetime.utcnow().isoformat(), chat_id, amount_usd)
        )
        return cur.lastrowid

def attach_invoice(order_id: int, invoice_id: str, invoice_url: str):
    with db() as conn:
        conn.execute(
            "UPDATE orders SET invoice_id=?, invoice_url=?, status=? WHERE id=?",
            (invoice_id, invoice_url, "invoice_created", order_id)
        )

def set_order_status(order_id: int, status: str):
    with db() as conn:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))

def get_order_by_invoice(invoice_id: str):
    with db() as conn:
        cur = conn.execute("SELECT * FROM orders WHERE invoice_id=?", (invoice_id,))
        return cur.fetchone()

def upsert_payment(payment_id: str, order_id: int, status: str, raw: dict):
    with db() as conn:
        conn.execute("""
        INSERT INTO payments (payment_id, order_id, status, raw_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(payment_id) DO UPDATE SET
            status=excluded.status,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """, (payment_id, order_id, status, json.dumps(raw, ensure_ascii=False), datetime.utcnow().isoformat()))

# ============ NOWPayments ============
NOWPAYMENTS_INVOICE_URL = "https://api.nowpayments.io/v1/invoice"

def verify_nowpayments_signature(raw_body: bytes, signature: str | None) -> bool:
    """
    NOWPayments signature is HMAC-SHA512 of sorted JSON body using IPN secret.
    Header: x-nowpayments-sig
    """
    if not signature:
        return False
    try:
        body_obj = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return False

    sorted_str = json.dumps(body_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode("utf-8"),
        sorted_str.encode("utf-8"),
        hashlib.sha512
    ).hexdigest()

    return hmac.compare_digest(digest, signature)

async def create_invoice(amount_usd: float, order_id: int) -> dict:
    payload = {
        "price_amount": amount_usd,
        "price_currency": PRICE_CURRENCY,
        "pay_currency": PAY_CURRENCY,
        "order_id": str(order_id),
        "order_description": f"Order #{order_id}",
        # Webhook is already set in your NOWPayments dashboard,
        # but keeping this here is also fine. If they ignore it, no problem.
        # "ipn_callback_url": "https://your-domain/ipn"
    }

    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(NOWPAYMENTS_INVOICE_URL, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()

# ============ FASTAPI (Webhook) ============
api = FastAPI()
tg_app: Application | None = None  # will be assigned later

@api.post("/ipn")
async def nowpayments_ipn(request: Request, x_nowpayments_sig: str | None = Header(default=None)):
    raw = await request.body()

    if not verify_nowpayments_signature(raw, x_nowpayments_sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(raw.decode("utf-8"))

    status = (data.get("payment_status") or data.get("status") or "").lower()
    payment_id = str(data.get("payment_id") or data.get("id") or "")
    invoice_id = str(data.get("invoice_id") or data.get("invoice") or "")

    if not payment_id or not invoice_id:
        return {"ok": True}

    order = get_order_by_invoice(invoice_id)
    if order:
        upsert_payment(payment_id, int(order["id"]), status, data)
        set_order_status(int(order["id"]), status)

        # Notify admin when payment confirmed/finished
        if tg_app and status in {"confirmed", "finished"}:
            await tg_app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"✅ تم الدفع للطلب #{order['id']}\n"
                    f"المبلغ: {order['amount_usd']} USD\n"
                    f"الحالة: {status}\n"
                    f"Payment ID: {payment_id}"
                )
            )

    return {"ok": True}

# ============ TELEGRAM BOT ============
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ هذا البوت خاص بالإدارة فقط")
        return
    await update.message.reply_text("✅ البوت شغال.\nاستخدم: /pay 5")

async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ هذا البوت خاص بالإدارة فقط")
        return

    if not context.args:
        await update.message.reply_text("اكتب المبلغ مثل: /pay 5")
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("المبلغ لازم يكون رقم صحيح مثل: /pay 5")
        return

    order_id = create_order(chat_id=update.message.chat_id, amount_usd=amount)

    try:
        inv = await create_invoice(amount, order_id)

        # NOWPayments invoice response usually contains: id + invoice_url
        invoice_id = str(inv.get("id") or inv.get("invoice_id") or "")
        invoice_url = inv.get("invoice_url") or inv.get("payment_url") or ""

        if not invoice_id or not invoice_url:
            set_order_status(order_id, "invoice_error")
            await update.message.reply_text(f"❌ صار خطأ بإنشاء الفاتورة.\nالرد: {inv}")
            return

        attach_invoice(order_id, invoice_id, invoice_url)

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("ادفع الآن", url=invoice_url)]])
        await update.message.reply_text(
            f"🧾 تم إنشاء فاتورة للطلب #{order_id}\n"
            f"المبلغ: {amount} USD\n"
            f"ارسل هذا الرابط للزبون:",
            reply_markup=kb
        )
        await update.message.reply_text(invoice_url)

    except httpx.HTTPStatusError as e:
        set_order_status(order_id, "invoice_http_error")
        await update.message.reply_text(f"❌ خطأ من NOWPayments:\n{e.response.text}")
    except Exception as e:
        set_order_status(order_id, "invoice_exception")
        await update.message.reply_text(f"❌ خطأ غير متوقع: {str(e)}")

# ============ RUN BOTH (WEB + BOT) ============
async def run_web():
    config = uvicorn.Config(api, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def run_bot():
    global tg_app
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CommandHandler("pay", pay_cmd))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling()
    # keep running
    await asyncio.Event().wait()

async def main():
    init_db()
    await asyncio.gather(run_web(), run_bot())

if __name__ == "__main__":
    asyncio.run(main())
