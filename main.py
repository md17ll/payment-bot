import os
import json
import hmac
import hashlib
import sqlite3
import threading
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
import uvicorn

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== ENV ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.getenv("NOWPAYMENTS_IPN_SECRET", "")

# خليها من Variables إذا بدك (افضل)
PRICE_CURRENCY = os.getenv("PRICE_CURRENCY", "usd")
PAY_CURRENCY = os.getenv("PAY_CURRENCY", "usdttrc20")

PORT = int(os.getenv("PORT", "8000"))

if not BOT_TOKEN or not ADMIN_ID or not NOWPAYMENTS_API_KEY or not NOWPAYMENTS_IPN_SECRET:
    raise RuntimeError("Missing env vars. Check Railway Variables.")

# ================== DB ==================
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

def get_order(order_id: int):
    with db() as conn:
        cur = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        return cur.fetchone()

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

def stats_summary():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
        paid = conn.execute("""
            SELECT COUNT(*) c FROM orders
            WHERE lower(status) IN ('confirmed','finished')
        """).fetchone()["c"]
        pending = conn.execute("""
            SELECT COUNT(*) c FROM orders
            WHERE lower(status) NOT IN ('confirmed','finished')
        """).fetchone()["c"]
        sum_all = conn.execute("SELECT COALESCE(SUM(amount_usd),0) s FROM orders").fetchone()["s"]
        sum_paid = conn.execute("""
            SELECT COALESCE(SUM(amount_usd),0) s FROM orders
            WHERE lower(status) IN ('confirmed','finished')
        """).fetchone()["s"]

    return {
        "total": total,
        "paid": paid,
        "pending": pending,
        "sum_all": float(sum_all),
        "sum_paid": float(sum_paid),
    }

# ================== NOWPayments ==================
NOWPAYMENTS_INVOICE_URL = "https://api.nowpayments.io/v1/invoice"

def verify_nowpayments_signature(raw_body: bytes, signature: str | None) -> bool:
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
    }
    headers = {"x-api-key": NOWPAYMENTS_API_KEY, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post(NOWPAYMENTS_INVOICE_URL, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()

# ================== WEB (FastAPI) ==================
app = FastAPI()
tg_app: Application | None = None

@app.get("/")
async def root():
    return {"ok": True, "service": "payment-bot"}

@app.post("/ipn")
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

        # إشعار للأدمن فقط عند الدفع النهائي
        if tg_app and status in {"confirmed", "finished"}:
            await tg_app.bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    f"✅ تم الدفع\n"
                    f"طلب #{order['id']} | {order['amount_usd']} USD\n"
                    f"الحالة: {status}\n"
                    f"Payment ID: {payment_id}"
                )
            )

    return {"ok": True}

def run_uvicorn():
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")

# ================== TELEGRAM (Admin only) ==================
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 إنشاء رابط دفع (مخصص)", callback_data="mkpay_custom")],
        [InlineKeyboardButton("📦 حالة الطلب", callback_data="order_status")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="stats")],
        [InlineKeyboardButton("ℹ️ مساعدة", callback_data="help")],
    ])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("❌ هذا البوت خاص بالإدارة فقط")
        return
    # تنظيف أي حالات انتظار
    context.user_data.pop("await_amount", None)
    context.user_data.pop("await_order_id", None)
    await update.message.reply_text("✅ أهلاً أدمن. اختر من الأزرار:", reply_markup=main_menu())

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(update):
        await q.edit_message_text("❌ هذا البوت خاص بالإدارة فقط")
        return

    data = q.data

    if data == "mkpay_custom":
        context.user_data["await_amount"] = True
        context.user_data.pop("await_order_id", None)
        await q.edit_message_text("✍️ اكتب المبلغ بالدولار (مثال: 5 أو 12.5)\n\n(للإلغاء اكتب: cancel)")
        return

    if data == "order_status":
        context.user_data["await_order_id"] = True
        context.user_data.pop("await_amount", None)
        await q.edit_message_text("📦 اكتب رقم الطلب (Order ID)\n\n(للإلغاء اكتب: cancel)")
        return

    if data == "stats":
        s = stats_summary()
        await q.edit_message_text(
            "📊 إحصائيات:\n"
            f"- إجمالي الطلبات: {s['total']}\n"
            f"- طلبات مدفوعة: {s['paid']}\n"
            f"- طلبات غير مدفوعة/قيد المعالجة: {s['pending']}\n"
            f"- مجموع كل الطلبات: {s['sum_all']:.2f} USD\n"
            f"- مجموع المدفوع: {s['sum_paid']:.2f} USD\n",
            reply_markup=main_menu()
        )
        return

    if data == "help":
        await q.edit_message_text(
            "ℹ️ شرح سريع:\n"
            "1) 💳 إنشاء رابط دفع (مخصص) → اكتب مبلغ بالدولار\n"
            "2) 📦 حالة الطلب → اكتب رقم الطلب\n"
            "3) 📊 إحصائيات → عرض ملخص الطلبات\n\n"
            "🔔 عند اكتمال الدفع (confirmed/finished) يصلك إشعار تلقائيًا.",
            reply_markup=main_menu()
        )
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text = (update.message.text or "").strip()

    # إلغاء أي وضع انتظار
    if text.lower() in {"cancel", "c", "stop"}:
        context.user_data.pop("await_amount", None)
        context.user_data.pop("await_order_id", None)
        await update.message.reply_text("✅ تم الإلغاء.", reply_markup=main_menu())
        return

    # انتظار مبلغ لإنشاء رابط
    if context.user_data.get("await_amount"):
        cleaned = text.replace(",", ".")
        try:
            amount = float(cleaned)
            if amount <= 0:
                raise ValueError()
        except Exception:
            await update.message.reply_text("❌ اكتب رقم صحيح (مثال: 5 أو 12.5) أو اكتب cancel للإلغاء")
            return

        context.user_data["await_amount"] = False
        await update.message.reply_text("⏳ جاري إنشاء رابط الدفع...")
        await make_payment_link(update.message.chat_id, amount, context)
        return

    # انتظار رقم طلب لعرض الحالة
    if context.user_data.get("await_order_id"):
        try:
            oid = int(text)
            if oid <= 0:
                raise ValueError()
        except Exception:
            await update.message.reply_text("❌ اكتب رقم طلب صحيح (مثال: 12) أو cancel للإلغاء")
            return

        context.user_data["await_order_id"] = False
        order = get_order(oid)
        if not order:
            await update.message.reply_text("❌ ما لقيت طلب بهذا الرقم.", reply_markup=main_menu())
            return

        status = (order["status"] or "unknown")
        msg = (
            f"📦 حالة الطلب #{order['id']}:\n"
            f"- المبلغ: {order['amount_usd']} USD\n"
            f"- الحالة: {status}\n"
        )
        if order["invoice_url"]:
            msg += f"- رابط الدفع: {order['invoice_url']}\n"

        await update.message.reply_text(msg, reply_markup=main_menu())
        return

async def make_payment_link(chat_id: int, amount: float, context: ContextTypes.DEFAULT_TYPE):
    order_id = create_order(chat_id=chat_id, amount_usd=amount)
    try:
        inv = await create_invoice(amount, order_id)
        invoice_id = str(inv.get("id") or inv.get("invoice_id") or "")
        invoice_url = inv.get("invoice_url") or inv.get("payment_url") or ""

        if not invoice_id or not invoice_url:
            set_order_status(order_id, "invoice_error")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ فشل إنشاء الفاتورة.\nالرد: {inv}",
                reply_markup=main_menu()
            )
            return

        attach_invoice(order_id, invoice_id, invoice_url)

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("ادفع الآن", url=invoice_url)]])
        text = f"🧾 طلب #{order_id}\nالمبلغ: {amount} USD\n🔗 رابط الدفع:"
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        await context.bot.send_message(chat_id=chat_id, text=invoice_url)

    except httpx.HTTPStatusError as e:
        set_order_status(order_id, "invoice_http_error")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ خطأ من NOWPayments:\n{e.response.text}",
            reply_markup=main_menu()
        )
    except Exception as e:
        set_order_status(order_id, "invoice_exception")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ خطأ غير متوقع: {str(e)}",
            reply_markup=main_menu()
        )

# ================== START ==================
def main():
    global tg_app
    init_db()

    # Start web server in background thread (for /ipn webhook)
    threading.Thread(target=run_uvicorn, daemon=True).start()

    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start_cmd))
    tg_app.add_handler(CallbackQueryHandler(on_button))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    tg_app.run_polling()

if __name__ == "__main__":
    main()
