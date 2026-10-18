import os
import io
import csv
import json
import sqlite3
import logging
import threading
import time
import asyncio
from flask import Flask, request, jsonify, render_template, send_file
from functools import wraps
from datetime import datetime, date, timedelta
from uuid import uuid4
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature  # >>> ADMIN PANEL: SignatureExpired/BadSignature were missing
from urllib.parse import urlencode
import urllib.request  # >>> ADMIN PANEL
import re  # >>> ADMIN PANEL
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputMediaPhoto, ReplyKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

# ─── WEBAPP ────────────────────────────────────────────────────────────────────

web_app = Flask(__name__)

# ─── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# ─── CONFIG & DATABASE ─────────────────────────────────────────────────────────
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
DB_PATH = os.getenv("DB_PATH")
ORDER_PAGE_SIZE = int(os.getenv("ORDER_PAGE_SIZE"))
VIP_MIN_INV = float(os.getenv("VIP_MIN_INV"))
DEFAULT_WARNING_THRESHOLD = int(os.getenv("DEFAULT_WARNING_THRESHOLD"))
WEBAPP_URL = os.getenv("WEBAPP_URL")
AUTH_SECRET_KEY = os.getenv("AUTH_SECRET_KEY")

SIGNER = URLSafeTimedSerializer(AUTH_SECRET_KEY)

# Parse comma-separated string to list of integers
ADMIN_USER_IDS = list(map(int, os.getenv("ADMIN_USER_IDS", "").split(",")))

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # orders table
    c.execute("""
      CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        name TEXT,
        contact TEXT,
        address TEXT,
        zip TEXT,
        product TEXT,
        qty INTEGER,
        total REAL,
        courier TEXT,
        delivery TEXT,
        payment TEXT,
        status TEXT,
        created_at TEXT,
        tracking TEXT DEFAULT ''
      )
    """)
    # stocks table
    c.execute("""
      CREATE TABLE IF NOT EXISTS stocks (
        product_key TEXT PRIMARY KEY,
        stock INTEGER
      )
    """)
    # referrals
    c.execute("""
      CREATE TABLE IF NOT EXISTS referrals (
        user_id TEXT PRIMARY KEY,
        code    TEXT UNIQUE
      )
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS user_referrals (
        referred_id TEXT PRIMARY KEY,
        referrer_id TEXT
      )
    """)
    # settings table
    c.execute("""
      CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
      )
    """)
    conn.commit()
    return conn

DB = init_db()

# ─── SETTINGS HELPERS ──────────────────────────────────────────────────────────
def get_setting(key: str, default=None):
    c = DB.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    c = DB.cursor()
    c.execute("REPLACE INTO settings(key,value) VALUES (?,?)", (key, value))
    DB.commit()

def get_warning_threshold() -> int:
    val = get_setting("warning_threshold")
    return int(val) if val and val.isdigit() else DEFAULT_WARNING_THRESHOLD


def get_global_theme() -> str:
    """Read the current theme from the DB (defaults to 'default')."""
    return get_setting("theme", "default")

def set_global_theme(theme: str):
    """Write a new global theme into settings."""
    set_setting("theme", theme)

def count_user_orders(user_id: str) -> int:
    c = DB.cursor()
    c.execute("SELECT COUNT(*) AS cnt FROM orders WHERE user_id=?", (user_id,))
    return c.fetchone()["cnt"] or 0

def count_all_orders() -> int:
    c = DB.cursor()
    c.execute("SELECT COUNT(*) AS cnt FROM orders")
    return c.fetchone()["cnt"] or 0

# ─── THEME HELPER ───────────────────────────────────────────────────────────

def get_image_path(info_path: str, context) -> str:
    """
    Given an original path like "img/product/foo.png",
    return "img/themes/{theme}/foo.png" where {theme} comes
    from the database (not per-user).
    """
    theme = get_global_theme()
    fname = os.path.basename(info_path)
    return os.path.join("img", "themes", theme, fname)


# ─── /set_theme COMMAND ──────────────────────────────────────────────────────

async def set_theme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # restrict by user
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to change the theme.")
    # 2) list dirs under img/themes
    themes_dir = os.path.join("img", "themes")
    try:
        themes = [d for d in os.listdir(themes_dir)
                  if os.path.isdir(os.path.join(themes_dir, d))]
    except FileNotFoundError:
        return await update.message.reply_text("⚠️ No themes folder found on disk.")

    # 3) /set_theme → list or select
    if not context.args:
        return await update.message.reply_text(
            f"Available themes: {', '.join(themes)}\n\n"
            "Usage: /set_theme <theme>"
        )

    choice = context.args[0]
    if choice not in themes:
        return await update.message.reply_text(
            f"❌ Unknown theme “{choice}”. Available: {', '.join(themes)}"
        )

    # 4) save globally
    set_global_theme(choice)
    await update.message.reply_text(
        f"✅ Global theme set to *{choice}*",
        parse_mode="Markdown"
    )


# ─── REFERRAL HELPERS ───────────────────────────────────────────────────────────
def get_or_create_referral_code(user_id: str) -> str:
    c = DB.cursor()
    c.execute("SELECT code FROM referrals WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if row:
        return row["code"]
    code = uuid4().hex[:6]
    c.execute("INSERT INTO referrals(user_id,code) VALUES (?,?)", (user_id, code))
    DB.commit()
    return code

def record_referral(referred_id: str, ref_code: str):
    c = DB.cursor()
    c.execute("SELECT user_id FROM referrals WHERE code=?", (ref_code,))
    row = c.fetchone()
    if not row or row["user_id"] == referred_id:
        return
    referrer = row["user_id"]
    c.execute("""
      INSERT OR IGNORE INTO user_referrals (referred_id, referrer_id)
      VALUES (?,?)
    """, (referred_id, referrer))
    DB.commit()

def get_referral_count(user_id: str) -> int:
    c = DB.cursor()
    c.execute("SELECT COUNT(*) AS cnt FROM user_referrals WHERE referrer_id=?", (user_id,))
    return c.fetchone()["cnt"] or 0

def get_referral_earnings(user_id: str) -> float:
    c = DB.cursor()
    c.execute("""
      SELECT SUM(o.total * 0.1) AS earnings
      FROM orders o
      JOIN user_referrals ur
        ON o.user_id = ur.referred_id
      WHERE ur.referrer_id=? AND o.status='Confirmed'
    """, (user_id,))
    return c.fetchone()["earnings"] or 0.0

# ─── STOCK HELPERS ─────────────────────────────────────────────────────────────
def get_stock(key: str) -> int:
    c = DB.cursor()
    c.execute("SELECT stock FROM stocks WHERE product_key=?", (key,))
    row = c.fetchone()
    return row["stock"] if row else 0

def set_stock(key: str, stock: int):
    c = DB.cursor()
    c.execute("REPLACE INTO stocks (product_key, stock) VALUES (?,?)", (key, stock))
    DB.commit()

def decrement_stock(key: str, qty: int):
    curr = get_stock(key)
    new  = max(0, curr - qty)
    set_stock(key, new)

# ─── ORDER HELPERS ─────────────────────────────────────────────────────────────
def add_order(user_id, name, contact, address, zipc,
              product, qty, total, courier, delivery, payment):
    dt = datetime.utcnow().strftime("%b %d, %Y")
    c  = DB.cursor()
    c.execute("""
      INSERT INTO orders
        (user_id,name,contact,address,zip,
         product,qty,total,courier,delivery,
         payment,status,created_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        user_id,name,contact,address,zipc,
        product,qty,total,courier,delivery,
        payment,"Pending",dt
    ))
    DB.commit()
    return c.lastrowid

def update_order_status(order_id, new_status):
    c = DB.cursor()
    c.execute("UPDATE orders SET status=? WHERE id=?", (new_status, order_id))
    DB.commit()

def save_tracking(order_id, tracking_num):
    c = DB.cursor()
    c.execute("UPDATE orders SET tracking=? WHERE id=?", (tracking_num, order_id))
    DB.commit()

def delete_order(order_id):
    c = DB.cursor()
    c.execute("DELETE FROM orders WHERE id=?", (order_id,))
    DB.commit()

def get_user_orders(user_id, page):
    if page < 1:
        return []
    ofs = (page-1)*ORDER_PAGE_SIZE
    c   = DB.cursor()
    c.execute("""
      SELECT *, created_at AS date
      FROM orders
      WHERE user_id=?
      ORDER BY id DESC
      LIMIT ? OFFSET ?
    """, (user_id, ORDER_PAGE_SIZE, ofs))
    return c.fetchall()

def get_all_orders(page):
    if page < 1:
        return []
    ofs = (page-1)*ORDER_PAGE_SIZE
    c   = DB.cursor()
    c.execute("""
      SELECT *, created_at AS date
      FROM orders
      ORDER BY id DESC
      LIMIT ? OFFSET ?
    """, (ORDER_PAGE_SIZE, ofs))
    return c.fetchall()

def get_order(order_id):
    c = DB.cursor()
    c.execute("SELECT *, created_at AS date FROM orders WHERE id=?", (order_id,))
    return c.fetchone()

def fetch_orders(status=None, page=1):
    ofs = (max(page,1)-1)*ORDER_PAGE_SIZE
    c = DB.cursor()
    if status and status.lower() != "all":
        c.execute("""
          SELECT *, created_at AS date FROM orders
          WHERE status=? 
          ORDER BY id DESC
          LIMIT ? OFFSET ?
        """, (status.capitalize(), ORDER_PAGE_SIZE, ofs))
    else:
        c.execute("""
          SELECT *, created_at AS date FROM orders
          ORDER BY id DESC
          LIMIT ? OFFSET ?
        """, (ORDER_PAGE_SIZE, ofs))
    return c.fetchall()

def fetch_all_orders():
    c = DB.cursor()
    c.execute("SELECT *, created_at AS date FROM orders ORDER BY id DESC")
    return c.fetchall()

def parse_order_date(r):
    try:
        return datetime.strptime(r["created_at"], "%b %d, %Y").date()
    except:
        return None

def format_order_list(rows, header):
    lines = [header]
    for r in rows:
        lines.append(
            f"A{r['id']:06d}: {r['product']} ×{r['qty']} — {r['status']} on {r['created_at']}"
        )
    return "\n".join(lines)

# ─── PRODUCT CONFIG ────────────────────────────────────────────────────────────
PRODUCTS = {

}

# >>> ADMIN PANEL: products saved in the DB so add/edit/delete survive a restart
def init_products_table():
    c = DB.cursor()
    c.execute("""
      CREATE TABLE IF NOT EXISTS products (
        key TEXT PRIMARY KEY, name TEXT, price REAL,
        min_qty INTEGER DEFAULT 1, image TEXT
      )
    """)
    DB.commit()
    c.execute("SELECT COUNT(*) AS n FROM products")
    if c.fetchone()["n"] == 0:
        # first run: copy the PRODUCTS dict above into the DB
        for k, v in PRODUCTS.items():
            c.execute("INSERT INTO products VALUES (?,?,?,?,?)",
                      (k, v["name"], v["price"], v["min_qty"], v["image"]))
        DB.commit()
    else:
        # next runs: DB is the source of truth
        PRODUCTS.clear()
        c.execute("SELECT * FROM products ORDER BY rowid")
        for r in c.fetchall():
            PRODUCTS[r["key"]] = {"name": r["name"], "price": r["price"],
                                  "min_qty": r["min_qty"], "image": r["image"]}

init_products_table()

# >>> ADMIN PANEL: maintenance helper
def is_maintenance() -> bool:
    return get_setting("maintenance", "0") == "1"



# ─── KEYBOARDS & BUILDERS ──────────────────────────────────────────────────────


# ─── BUILD PRODUCT CARD ────────────────────────────────────────────────────────
def build_product_card(key: str, qty: int):
    info  = PRODUCTS[key]
    total = info["price"] * qty
    stock = get_stock(key)
    caption = (
        f"🎭 *{info['name']}*\n\n"
        f"*Qty:* {qty}   •   *Total:* PHP {total:.2f}\n"
        f"*Stocks Available:* {stock}\n\n"
        "_Adjust qty then confirm to proceed_"
    )
    rows = [[
        InlineKeyboardButton("➖", callback_data="DECR_QTY"),
        InlineKeyboardButton(str(qty), callback_data="QTY"),
        InlineKeyboardButton("➕", callback_data="INCR_QTY"),
    ]]
    if stock > 0:
        rows.append([
            InlineKeyboardButton("✅ Confirm Order", callback_data="CONFIRM_ORDER"),
            InlineKeyboardButton("❌ Cancel",        callback_data="CANCEL_ORDER"),
        ])
    else:
        rows.append([
            InlineKeyboardButton("❌ Not Available", callback_data="NO_STOCK"),
            InlineKeyboardButton("❌ Cancel",        callback_data="CANCEL_ORDER"),
        ])
    return caption, InlineKeyboardMarkup(rows)

# ─── SHIPPING & PAYMENT SUMMARY ────────────────────────────────────────────────
def build_shipping_summary_and_kb(data: dict):
    text = (
        "📝 *Shipping Information*\n"
        f"📛 Full Name: {data.get('shipping_name','')}\n"
        f"🏠 Address:   {data.get('shipping_address','')}\n"
        f"☎️ Contact:   {data.get('shipping_contact','')}\n"
        f"📍 Landmark:  {data.get('shipping_zip','')}\n\n"
        f"📦 Product:   {data.get('selected_product','')}\n"
        f"🔢 Quantity:  {data.get('product_qty',0)}\n\n"
        f"🚚 Courier:   *{data.get('courier','Select below')}*\n"
        f"⚡️ Delivery: *{data.get('delivery','Select below')}*\n"
        f"💰 Payment:   *{data.get('payment','Select below')}*"
    )
    def mk(lbl, cd, sel):
        return InlineKeyboardButton(("✅ " if sel else "") + lbl, callback_data=cd)

    cour = [
        #mk("Grab",      "CR_Grab",     data.get("courier")=="Grab"),
        mk("Lalamove",  "CR_Lalamove", data.get("courier")=="Lalamove"),
        #mk("NinjaVan",  "CR_NinjaVan", data.get("courier")=="NinjaVan"),
        mk("J&T",       "CR_J&T",      data.get("courier")=="J&T"),
        mk("JRS",       "CR_JRS",      data.get("courier")=="JRS"),
    ]
    deli = [
        mk("Same Day","DT_SameDay", data.get("delivery")=="Same Day"),
        mk("Normal",  "DT_Normal",  data.get("delivery")=="Normal"),
        mk("Custom",  "DT_Custom",  data.get("delivery")=="Custom"),
    ]
    paym = [
        mk("Gcash",  "PM_Gcash",  data.get("payment")=="Gcash"),
        #mk("Maya",   "PM_Maya",   data.get("payment")=="Maya"),
        #mk("Crypto", "PM_Crypto", data.get("payment")=="Crypto"),
    ]
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🚚 Courier:", callback_data="IGNORE_COURIER")],
        cour,
        [InlineKeyboardButton("⚡️ Delivery:", callback_data="IGNORE_DELIVERY")],
        deli,
        [InlineKeyboardButton("💰 Payment:",   callback_data="IGNORE_PAYMENT")],
        paym,
        [InlineKeyboardButton("✅ Proceed to Pay", callback_data="CONFIRM_PAYMENT")],
    ])

# ─── ADMIN INLINE KEYS ─────────────────────────────────────────────────────────
def admin_kb_for(order_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm",   callback_data=f"ADMIN_CONFIRM_{order_id}"),
            InlineKeyboardButton("❌ Cancel",    callback_data=f"ADMIN_CANCEL_{order_id}"),
            InlineKeyboardButton("📦 Delivered", callback_data=f"ADMIN_DELIVERED_{order_id}"),
        ],
        [InlineKeyboardButton("🚚 Send Tracking Number", callback_data=f"SEND_TRACKING_{order_id}")],
        [InlineKeyboardButton("🗑 Delete Order",           callback_data=f"ADMIN_DELETE_{order_id}")],
    ])

# ─── RENDER ORDERS PAGE ────────────────────────────────────────────────────────
def render_orders_page(rows, page, total_pages, prefix="● *Your Recent Orders*"):
    r   = rows[0]
    dot = {
        "Cancelled":"🔴",
        "Pending":"🟡",
        "Confirmed":"🟢",
        "Delivered":"🚚"
    }.get(r["status"], "⚪️")

    text = (
        f"{prefix}\n"
        f"Page {page} – {total_pages}\n\n"
        f"📦 *Product:* {r['product']}\n"
        f"📅 *Date:*    {r['date']}\n"
        f"🔢 *Qty:*     {r['qty']} – PHP {r['total']:.2f}\n"
        f"🚚 *Delivery:* {r['delivery']}\n"
        f"🔖 *Status:*   {dot} {r['status']}\n"
        f"🆔 *Order ID:* A{r['id']:06d}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚚 Track",   callback_data="TRACK"),
            InlineKeyboardButton("🔍 Details", callback_data="DETAILS"),
            InlineKeyboardButton("🔄 Refresh", callback_data="REFRESH"),
        ],
        [
            InlineKeyboardButton("◀ Prev",     callback_data=f"PAGE_{page-1}"),
            InlineKeyboardButton("Next ▶",     callback_data=f"PAGE_{page+1}"),
            InlineKeyboardButton("☎️ Support", url="https://t.me/"),
        ],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="BACK_MAIN_MENU")],
    ])
    return text, kb

# ─── EXISTING ADMIN COMMANDS ──────────────────────────────────────────────────
async def stocklist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    lines = ["Stock Levels:"] + [
        f"{v['name']}: {get_stock(k)}" for k,v in PRODUCTS.items()
    ]
    await update.message.reply_text("\n".join(lines))

async def admindashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")

    # Securely generate a signed auth token
    user_id = update.effective_user.id
    auth_token = SIGNER.dumps({"user_id": user_id})

    # Build the dashboard URL with secure token
    query_params = urlencode({"token": auth_token, "user_id": user_id})
    full_url = f"{WEBAPP_URL}/adminpanel?{query_params}"

    # Inline keyboard with Web App button
    keyboard = [
        [InlineKeyboardButton("📊 Open Admin Dashboard", web_app=WebAppInfo(url=full_url))]
    ]

    await update.message.reply_text(
        "✅ Welcome, Admin!\nTap the button below to access your dashboard:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def productnames_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    lines = ["Available Products:"] + [v['name'] for v in PRODUCTS.values()]
    await update.message.reply_text("\n".join(lines))

async def stocklist_reset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # restrict by user
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    
    # Reset stock to 0 for all products
    c = DB.cursor()
    c.execute("UPDATE stocks SET stock = 0")
    DB.commit()
    
    await update.message.reply_text("✅ All stocks have been reset to zero.")

async def orderlist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # restrict by user
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    
    c = DB.cursor()
    c.execute("SELECT id, product FROM orders")
    orders = c.fetchall()
    
    if not orders:
        return await update.message.reply_text("❌ No orders found.")
    
    order_list = "\n".join([f"A{order['id']:06d}: {order['product']}" for order in orders])
    await update.message.reply_text(f"📋 *All Orders*\n\n{order_list}", parse_mode="Markdown")

async def stock_all_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # restrict by user
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    
    # Check if the amount argument is provided and is a valid number
    if len(context.args) != 1 or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /stock all <amount>")
    
    amount = int(context.args[0])
    
    # Add the specified amount to all product stocks
    c = DB.cursor()
    c.execute("UPDATE stocks SET stock = stock + ?", (amount,))
    DB.commit()
    
    await update.message.reply_text(f"✅ {amount} added to all product stocks.")



async def restock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # restrict by user instead of chat
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to restock.")
    args = context.args
    if len(args) < 2:
        return await update.message.reply_text("Usage: /restock <Product Name> <Amount>")
    amt_str = args[-1]
    name    = " ".join(args[:-1]).lower()
    key     = next((k for k,v in PRODUCTS.items() if v['name'].lower()==name), None)
    if not key:
        return await update.message.reply_text("❌ Invalid product name.")
    try:
        amt = int(amt_str)
    except:
        return await update.message.reply_text("❌ Amount must be an integer.")
    new = get_stock(key) + amt
    set_stock(key, new)
    await update.message.reply_text(f"✅ Restocked {PRODUCTS[key]['name']} to {new}.")

async def admincommands_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    
    await update.message.reply_text(
        "/stocklist_reset — Reset all stock to zero\n"
        "/orderlist — List all order IDs and products\n"
        "/stock_all <amount> — Add stock to all products at once\n"
        "/set_theme <theme> — Set the global theme for the bot\n"
        "/stocklist — Show stock levels\n"
        "/productnames — List all product names\n"
        "/restock <Product Name> <amount> — Add stock\n"
        "/orders [status] [page] — Show orders with pagination\n"
        "/order <order_id> — Show details of a specific order\n"
        "/user_orders <user_id> [page] — Show orders of a specific user\n"
        "/orders_date <YYYY-MM-DD> [status] — Show orders for a specific date\n"
        "/stats revenue [today|week|month] — Show revenue statistics\n"
        "/top_products [today|week|month] — Show top products sold\n"
        "/low_stock [threshold] — Show products below the low stock threshold\n"
        "/export_orders [status|all] [csv|json] — Export orders in CSV/JSON format\n"
        "/bulk_update_status <status> <id1> [id2] … — Update status for multiple orders\n"
        "/stock_alerts — Show products that need restocking\n"
        "/set_warning_threshold <n> — Set the warning threshold for low stock\n"
        "/admindashboard - Sends the dashboard app\n"
        "/admincommands — Show this list"
    )


async def bang_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    txt = update.message.text.strip()
    if not txt.startswith("!"):
        return
    parts = txt[1:].split()
    cmd   = parts[0].lower()
    if cmd == "stocklist":
        return await stocklist_handler(update, context)
    if cmd == "productnames":
        return await productnames_handler(update, context)
    if cmd == "restock":
        return await restock_handler(update, context)
    if cmd == "admincommands":
        return await admincommands_handler(update, context)
    return await update.message.reply_text("❓ Unknown command. Send !admincommands for help.")

# ─── NEW ADMIN COMMANDS ────────────────────────────────────────────────────────
async def orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    args = context.args
    status = args[0] if args else None
    page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    rows = fetch_orders(status, page)
    if not rows:
        return await update.message.reply_text("No orders found.")
    hdr = f"/orders — Status: {status or 'all'} | Page {page}"
    await update.message.reply_text(format_order_list(rows, hdr))

async def order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    if not context.args:
        return await update.message.reply_text("Usage: /order <order_id>")
    oid = int(context.args[0])
    row = get_order(oid)
    if not row:
        return await update.message.reply_text("Order not found.")
    text = "\n".join(f"{k}: {row[k]}" for k in row.keys() if k!="id")
    await update.message.reply_text(f"A{oid:06d} Details:\n{text}")

async def user_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    if not context.args:
        return await update.message.reply_text("Usage: /user_orders <user_id> [page]")
    user_id = context.args[0]
    page = int(context.args[1]) if len(context.args)>1 and context.args[1].isdigit() else 1
    rows = get_user_orders(user_id, page)
    if not rows:
        return await update.message.reply_text("No orders for that user.")
    hdr = f"/user_orders — User: {user_id} | Page {page}"
    await update.message.reply_text(format_order_list(rows, hdr))

async def orders_date_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    if not context.args:
        return await update.message.reply_text("Usage: /orders_date <YYYY-MM-DD> [status]")
    try:
        target = datetime.strptime(context.args[0], "%Y-%m-%d").date()
    except:
        return await update.message.reply_text("Invalid date format.")
    status = context.args[1] if len(context.args)>1 else None
    all_rows = fetch_all_orders()
    rows = [r for r in all_rows
            if parse_order_date(r)==target and (not status or r["status"].lower()==status.lower())]
    if not rows:
        return await update.message.reply_text("No orders on that date.")
    hdr = f"/orders_date — {target} | Status: {status or 'all'}"
    await update.message.reply_text(format_order_list(rows, hdr))

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    if len(context.args)<1 or context.args[0].lower()!="revenue":
        return await update.message.reply_text("Usage: /stats revenue [today|week|month]")
    period = context.args[1].lower() if len(context.args)>1 else "today"
    today = date.today()
    if period=="today":
        start = today
    elif period=="week":
        start = today - timedelta(days=6)
    elif period=="month":
        start = today.replace(day=1)
    else:
        return await update.message.reply_text("Period must be today, week, or month.")
    total = 0.0
    for r in fetch_all_orders():
        rdate = parse_order_date(r)
        if rdate and start <= rdate <= today and r["status"]!="Cancelled":
            total += r["total"]
    await update.message.reply_text(f"Revenue ({period}): PHP {total:.2f}")

async def top_products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    period = context.args[0].lower() if context.args else "today"
    today = date.today()
    if period=="today":
        start = today
    elif period=="week":
        start = today - timedelta(days=6)
    elif period=="month":
        start = today.replace(day=1)
    else:
        return await update.message.reply_text("Usage: /top_products [today|week|month]")
    counts = {}
    for r in fetch_all_orders():
        rdate = parse_order_date(r)
        if rdate and start <= rdate <= today and r["status"]!="Cancelled":
            counts[r["product"]] = counts.get(r["product"],0) + r["qty"]
    if not counts:
        return await update.message.reply_text("No sales in that period.")
    top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [f"Top Products ({period}):"]
    for prod, cnt in top:
        lines.append(f"{prod}: {cnt} sold")
    await update.message.reply_text("\n".join(lines))

async def low_stock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    thresh = int(context.args[0]) if context.args and context.args[0].isdigit() else get_warning_threshold()
    lows = [(k,get_stock(k)) for k in PRODUCTS if get_stock(k)<=thresh]
    if not lows:
        return await update.message.reply_text("No products below threshold.")
    lines = [f"Low Stock (≤{thresh}):"]
    for k,stk in lows:
        lines.append(f"{PRODUCTS[k]['name']}: {stk}")
    await update.message.reply_text("\n".join(lines))

async def export_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    if len(context.args)<2 or context.args[1].lower() not in ("csv","json"):
        return await update.message.reply_text("Usage: /export_orders [status|all] [csv|json]")
    status, fmt = context.args[0], context.args[1].lower()
    rows = fetch_orders(status, 1) if status.lower()!="all" else fetch_all_orders()
    data = [dict(r) for r in rows]
    if fmt=="json":
        b = json.dumps(data, indent=2).encode()
        bio = io.BytesIO(b)
        bio.name = f"orders_{status or 'all'}.json"
        await update.message.reply_document(document=bio)
    else:
        bio = io.StringIO()
        writer = csv.DictWriter(bio, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
        b2 = io.BytesIO(bio.getvalue().encode())
        b2.name = f"orders_{status or 'all'}.csv"
        await update.message.reply_document(document=b2)

async def bulk_update_status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    if len(context.args)<2:
        return await update.message.reply_text("Usage: /bulk_update_status <status> <id1> [id2] …")
    new_status = context.args[0].capitalize()
    ids = []
    for tok in context.args[1:]:
        try:
            ids.append(int(tok.lstrip("A").lstrip("a")))
        except:
            pass
    updated = []
    for oid in ids:
        update_order_status(oid, new_status)
        updated.append(f"A{oid:06d}")
    await update.message.reply_text(f"Updated {', '.join(updated)} → {new_status}")

async def stock_alerts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    thresh = get_warning_threshold()
    lows = [(PRODUCTS[k]['name'], get_stock(k)) for k in PRODUCTS if get_stock(k)<=thresh]
    if not lows:
        return await update.message.reply_text("No stock alerts.")
    lines = [f"Stock Alerts (≤{thresh}):"]
    for name,stk in lows:
        lines.append(f"{name}: {stk}")
    await update.message.reply_text("\n".join(lines))

async def set_warning_threshold_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("❌ You’re not allowed to do that.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Usage: /set_warning_threshold <number>")
    n = int(context.args[0])
    set_setting("warning_threshold", str(n))
    await update.message.reply_text(f"Warning threshold set to {n}")

# ─── CORE BOT HANDLERS ────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # >>> ADMIN PANEL: maintenance mode
    if is_maintenance() and update.effective_user.id not in ADMIN_USER_IDS:
        return await update.message.reply_text("🛠 We're updating the shop right now. Please check back soon!")
    user_id = str(update.effective_user.id)
    get_or_create_referral_code(user_id)
    if context.args and context.args[0].startswith("r-"):
        record_referral(user_id, context.args[0][2:])

    # ── PRESERVE THEME ───────────────────────────────────────
    old_theme = context.user_data.get("theme")

    logo = get_image_path("img/logogg.png", context)
    sent = await update.message.reply_photo(
        photo=open(logo, "rb"),
        caption="👋 Welcome to Cubes & Seas! How can we help you today?",
        reply_markup=main_menu_kb()
    )

    # clear all user_data (no per-user theme logic here)
    context.user_data.clear()
    context.user_data["menu_msg_id"] = sent.message_id

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    # >>> ADMIN PANEL: maintenance mode (must run BEFORE the first query.answer())
    if is_maintenance() and query.from_user.id not in ADMIN_USER_IDS:
        return await query.answer("🛠 We're updating the shop. Please try again later.", show_alert=True)
    await query.answer()
    a       = query.data
    chat_id = query.message.chat.id
    uid     = str(query.from_user.id)

    async def delete_old_menu():
        if "menu_msg_id" in context.user_data:
            try:
                await context.bot.delete_message(chat_id, context.user_data["menu_msg_id"])
            except:
                pass

    # ─── HOW TO PURCHASE ─────────────────────────────────────────────────────────
    if a == "HOW_TO_PURCHASE":
        last = context.user_data.get("howto_ts", 0)
        now  = time.time()
        if now - last < 50:
            return   # still in cooldown, silently ignore
        context.user_data["howto_ts"] = now

        # 1) flip the inline button to “Please wait…”
        wait_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛒 Order Now",       callback_data="ORDER_NOW")],
            [InlineKeyboardButton("📦 Products",        callback_data="PRODUCTS"),
            InlineKeyboardButton("✅ Vouchers",        callback_data="VOUCHERS")],
            [InlineKeyboardButton("🧾 My Orders",       callback_data="MY_ORDER"),
            InlineKeyboardButton("☎️ Support",         url="https://t.me/")],
            [InlineKeyboardButton("🤝 Referral",        callback_data="REFERRAL")],
            [InlineKeyboardButton("💎 VIP Inquiry",     callback_data="VIP")],
            [InlineKeyboardButton("⌛️ Please wait for instructions...", callback_data="HOW_TO_PURCHASE")],
        ])
        await query.edit_message_reply_markup(reply_markup=wait_kb)

        # 2) send the video (no caption) and keep the Message object
        vid_msg = await query.message.reply_video(
            video=open("vid/instructions.mp4", "rb")
        )

        # 3) schedule it to auto‐delete after 3 minutes
        async def _auto_del():
            await asyncio.sleep(180)
            try:
                await context.bot.delete_message(chat_id, vid_msg.message_id)
            except:
                pass
        asyncio.create_task(_auto_del())

        # 4) immediately restore the normal keyboard
        await query.edit_message_reply_markup(reply_markup=main_menu_kb())
        return





    # ─── REFERRAL DASHBOARD ───────────────────────────────────────────────────
    if a == "REFERRAL":
        code     = get_or_create_referral_code(uid)
        invited  = get_referral_count(uid)
        earnings = get_referral_earnings(uid)
        botname  = context.bot.username
        link     = f"https://t.me/{botname}?start=r-{code}"

        caption = (
            "🤝 *Referral System*\n\n"
            "Invite your friends and earn rewards when they place orders!\n\n"
            "• Receive 10% commission on each confirmed order from your referrals.\n\n"
            f"💰 *Earnings:* PHP {earnings:.2f}\n"
            f"👥 *Invited users:* {invited}\n\n"
            f"*Your referral code:* `{code}`\n"
            f"*Your referral link:* `{link}`"
        )
        sent = await query.edit_message_caption(
            caption=caption,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Back", callback_data="BACK_MAIN")]
            ])
        )
        # make sure we delete this next time
        context.user_data["menu_msg_id"] = sent.message_id
        return

    # ─── VIP INQUIRY ────────────────────────────────────────────────────────────
    if a == "VIP":
        return await query.edit_message_caption(
            caption="💎 *VIP INVESTMENT PORTAL*\n\n"
                    "Secure your investment today to unlock exclusive perks, profit sharing, and early access to drops.\n\n"
                    f"Minimum Investment: ₱{VIP_MIN_INV:,.2f}",
            parse_mode="Markdown",
            reply_markup=vip_main_kb()
        )
    if a in ("VIP_BUSINESS","VIP_ASSET","VIP_PRODUCT"):
        m = {
            "VIP_BUSINESS":"Business Ventures",
            "VIP_ASSET":"Asset Growth",
            "VIP_PRODUCT":"Product Expansion"
        }[a]
        return await query.edit_message_caption(
            caption=f"💼 *{m}*\n\n"
                    f"You must invest at least ₱{VIP_MIN_INV:,.2f} to unlock {m}.",
            parse_mode="Markdown",
            reply_markup=vip_detail_kb()
        )
    if a == "VIP_SCHEDULE":
        return await query.edit_message_caption(
            caption="📅 *Schedule an appointment*\n\n"
                    "Reach out to @ to book your VIP consultation.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 @", url="https://t.me/")],
                [InlineKeyboardButton("◀️ Back",    callback_data="VIP")],
            ])
        )

    # ─── NO STOCK ALERT ──────────────────────────────────────────────────────────
    if a == "NO_STOCK":
        return await query.answer("Sorry, this product is out of stock.", show_alert=True)

    # ─── SEND_TRACKING PROMPT ────────────────────────────────────────────────────
    if a.startswith("SEND_TRACKING_"):
        oid = int(a.split("_")[2])
        context.user_data["awaiting_tracking"] = oid
        return await query.message.reply_text("Type the tracking number now:")

    # ─── ORDER NOW QUICK MENU ────────────────────────────────────────────────────

    # ─── VOUCHERS ───────────────────────────────────────────────────────────────
    if a == "VOUCHERS":
        return await query.edit_message_caption(
            caption="🎫 *Vouchers*\nJoin our group for exclusive deals:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎟️ Join Voucher Group", url="")],
                [InlineKeyboardButton("◀️ Back", callback_data="BACK_MAIN")]
            ])
        )

    # ─── SUPPORT ─────────────────────────────────────────────────────────────────
    if a == "SUPPORT":
        # Opens 
        return await query.edit_message_caption(
            caption="💬 *Support*\nNeed help? Reach out to @:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 @", url="https://t.me/")],
                [InlineKeyboardButton("◀️ Back",    callback_data="BACK_MAIN")]
            ])
        )

    # ─── REFRESH (My Orders) ─────────────────────
    if a == "REFRESH" and "order_page" in context.user_data:
        page = context.user_data["order_page"]
        rows = get_user_orders(uid, page)
        if not rows:
            return await query.answer()
        await query.message.delete()

        # recalc pagination
        total_orders = count_user_orders(uid)
        total_pages  = max(1, (total_orders + ORDER_PAGE_SIZE - 1) // ORDER_PAGE_SIZE)
        text, kb = render_orders_page(rows, page, total_pages)

        # pick the product image dynamically
        prod_name = rows[0]["product"]
        key = next((k for k,v in PRODUCTS.items() if v["name"] == prod_name), None)

        if key:
            img_path = get_image_path(PRODUCTS[key]["image"], context)
            sent = await context.bot.send_photo(
                chat_id,
                photo=open(img_path, "rb"),
                caption=text,
                parse_mode="Markdown",
                reply_markup=kb
            )
        else:
            sent = await context.bot.send_message(
                chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=kb
            )

        context.user_data["menu_msg_id"] = sent.message_id
        return
    
    # ─── BACK TO MAIN MENU ───────────────────────────────────────────────────────
    if a in ("BACK_MAIN","BACK_MAIN_MENU"):
        # 1) remember the theme so we can restore it after clearing
        old_theme = context.user_data.get("theme")

        # 2) delete whatever submenu/referral/menu we had up
        await delete_old_menu()

        # 3) re-send the main menu
        logo = get_image_path("img/logogg.png", context)
        sent = await context.bot.send_photo(
            chat_id,
            photo=open(logo, "rb"),
            caption="👋 Welcome back! What can we do for you today?",
            reply_markup=main_menu_kb()
        )

        # 4) clear everything (including the old menu_msg_id), then restore only what we need
        context.user_data.clear()
        context.user_data["menu_msg_id"] = sent.message_id
        if old_theme is not None:
            context.user_data["theme"] = old_theme

        return
    
    # ─── PRODUCT NAVIGATION ──────────────────────────────────────────────────────

    # ─── PRODUCT SELECTION ───────────────────────────────────────────────────────
    if a in PRODUCTS:
        key  = a
        info = PRODUCTS[key]
        context.user_data["selected_product_key"] = key
        context.user_data["selected_product"]     = info["name"]
        context.user_data["product_qty"]          = info["min_qty"]
        cap, kb = build_product_card(key, info["min_qty"])

        # First delete any previous card
        old_card_id = context.user_data.pop("card_msg_id", None)
        if old_card_id:
            try:
                await context.bot.delete_message(chat_id, old_card_id)
            except:
                pass

        # Now send the new card
        img_path = get_image_path(info["image"], context)
        try:
            sent = await context.bot.send_photo(
                chat_id,
                open(img_path, "rb"),
                caption=cap,
                parse_mode="Markdown",
                reply_markup=kb
            )
        except FileNotFoundError:
            sent = await context.bot.send_message(
                chat_id, cap, parse_mode="Markdown", reply_markup=kb
            )

        # Remember this one for next time
        context.user_data["card_msg_id"] = sent.message_id
        return

    # ─── ADJUST QTY ──────────────────────────────────────────────────────────────
    if a in ("INCR_QTY","DECR_QTY"):
        key = context.user_data.get("selected_product_key")
        if not key:
            return
        qty   = context.user_data["product_qty"]
        min_q = PRODUCTS[key]["min_qty"]
        qty   = qty + 1 if a=="INCR_QTY" else max(min_q, qty-1)
        context.user_data["product_qty"] = qty
        cap, kb = build_product_card(key, qty)
        return await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=context.user_data["card_msg_id"],
            caption=cap,
            parse_mode="Markdown",
            reply_markup=kb
        )

    # ─── CANCEL ORDER ────────────────────────────────────────────────────────────
    if a == "CANCEL_ORDER":
        # try to delete the product-card message, but ignore errors
        card_id = context.user_data.pop("card_msg_id", None)
        if card_id:
            try:
                await context.bot.delete_message(chat_id, card_id)
            except:
                pass

        # clean up only the keys related to that card
        for k in ("selected_product_key", "selected_product", "product_qty"):
            context.user_data.pop(k, None)

        # give the user a non-intrusive acknowledgement
        return await query.answer("Order cancelled.", show_alert=False)

    # ─── CONFIRM ORDER ───────────────────────────────────────────────────────────
    if a == "CONFIRM_ORDER":
        # safely delete card
        card_id = context.user_data.pop("card_msg_id", None)
        if card_id:
            try:
                await context.bot.delete_message(chat_id, card_id)
            except:
                pass

        prod = context.user_data["selected_product"]
        qty  = context.user_data["product_qty"]
        sent = await context.bot.send_message(
            chat_id,
            text=(
                f"📦 *Order Summary*\n"
                f"*Product:* {prod}\n"
                f"*Quantity:* {qty}\n\n"
                "📝 *Shipping Information*\n"
                "Enter your *full name*:"
            ),
            parse_mode="Markdown"
        )
        context.user_data["order_step"]  = "name"
        context.user_data["menu_msg_id"] = sent.message_id
        return

    # ─── IGNORE PLACEHOLDERS ─────────────────────────────────────────────────────
    if a.startswith("IGNORE_"):
        msgs = {
            "IGNORE_COURIER":"Choose courier",
            "IGNORE_DELIVERY":"Choose delivery type",
            "IGNORE_PAYMENT":"Choose payment method",
        }
        return await query.answer(msgs.get(a,""), show_alert=False)

    # ─── SHIPPING OPTIONS ─────────────────────────────────────────────────────────
    if a.startswith(("CR_","DT_","PM_")):
        if a.startswith("CR_"):
            context.user_data["courier"]  = a.split("_",1)[1]
        if a.startswith("DT_"):
            v = a.split("_",1)[1]
            context.user_data["delivery"] = "Same Day" if v=="SameDay" else v
        if a.startswith("PM_"):
            context.user_data["payment"]  = a.split("_",1)[1]
        txt, kb = build_shipping_summary_and_kb(context.user_data)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=context.user_data["menu_msg_id"],
            text=txt,
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    # ─── CONFIRM & SHOW QR ───────────────────────────────────────────────────────
    if a == "CONFIRM_PAYMENT":
        if context.user_data.get("order_step") != "choose":
            return await query.answer()
        context.user_data["order_step"] = "payment_initiated"
        await context.bot.delete_message(chat_id, context.user_data["menu_msg_id"])

        n   = context.user_data["shipping_name"]
        ad  = context.user_data["shipping_address"]
        ct  = context.user_data["shipping_contact"]
        zp  = context.user_data["shipping_zip"]
        pr  = context.user_data["selected_product"]
        qv  = context.user_data["product_qty"]
        price = PRODUCTS[context.user_data["selected_product_key"]]["price"]
        amt = qv * price
        context.user_data["amt"] = amt

        caption = (
            "💳 *Gcash QR*\nScan & pay the total amount.\n\n"
            "After paying, send a screenshot before tapping *Paid*.\n\n"
            "📝 *Shipping Info*\n"
            f"📛 Name: {n}\n"
            f"🏠 Address: {ad}\n"
            f"☎️ Contact: {ct}\n"
            f"📍 Landmark: {zp}\n\n"
            f"📦 Product: {pr}\n"
            f"🔢 Qty: {qv}\n"
            f"💵 Amount Due: PHP {amt:.2f}"
        )
        sent = await context.bot.send_photo(
            chat_id, open("img/QR/","rb"),
            caption=caption, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📸 Send payment screenshot", callback_data="AWAIT_SCREENSHOT")
            ]])
        )
        context.user_data["order_step"]  = "await_screenshot"
        context.user_data["menu_msg_id"] = sent.message_id
        context.user_data["admin_text"] = (
            "📦 New Order Received\n"
            f"👤 Name: {n}\n"
            f"☎️ Contact: {ct}\n"
            f"🏠 Address: {ad}\n"
            f"📍 Landmark: {zp}\n"
            f"📦 Product: {pr}\n"
            f"🔢 Qty: {qv}\n"
            f"💵 Due: PHP {amt:.2f}\n"
            f"🚚 Courier: {context.user_data.get('courier','')}\n"
            f"⚡️ Delivery: {context.user_data.get('delivery','')}\n"
            f"💰 Payment: {context.user_data.get('payment','')}"
        )
        return

    # ─── AWAIT SCREENSHOT ALERT ─────────────────────────────────────────────────
    if a == "AWAIT_SCREENSHOT":
        return await query.answer("Please send your payment screenshot now.", show_alert=True)

    # ─── PAID → FINALIZE ──────────────────────────────────────────────────────────
    if a == "PAID":
        # Only proceed if we’ve actually received a screenshot
        if context.user_data.get("order_step") != "screenshot_received":
            return await query.answer("Send screenshot first!", show_alert=True)

        # 1) delete the entire QR message (photo + caption + keyboard)
        try:
            await query.message.delete()
        except:
            pass

        # 2) confirm to the user
        await context.bot.send_message(
            chat_id,
            f"🎉 *Order Received!*\n\nThank you, {context.user_data['shipping_name']}! We’ve received your order.",
            parse_mode="Markdown"
        )

        # 3) show them the main-menu buttons
        sent_main = await context.bot.send_message(
            chat_id,
            "🏠 Return to main:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Main Menu",    callback_data="BACK_MAIN_MENU"),
                InlineKeyboardButton("🧾 My Orders",    callback_data="MY_ORDER"),
            ]])
        )
        context.user_data["menu_msg_id"] = sent_main.message_id
        context.user_data.pop("order_step", None)

        # 4) finalize the order in your DB and notify admin
        oid = add_order(
            uid,
            context.user_data["shipping_name"],
            context.user_data["shipping_contact"],
            context.user_data["shipping_address"],
            context.user_data["shipping_zip"],
            context.user_data["selected_product"],
            context.user_data["product_qty"],
            context.user_data["amt"],
            context.user_data.get("courier",""),
            context.user_data.get("delivery",""),
            context.user_data.get("payment","")
        )
        decrement_stock(
            context.user_data["selected_product_key"],
            context.user_data["product_qty"]
        )
        await context.bot.send_message(
            ADMIN_GROUP_ID,
            context.user_data["admin_text"] + f"\n🆔 ID: A{oid:06d}",
            parse_mode="Markdown",
            reply_markup=admin_kb_for(oid)
        )

        return

    # ─── CANCEL PAYMENT ─────────────────────────────────────────────────────────
    if a == "CANCEL_PAYMENT":
        # delete everything we showed before
        await delete_old_menu()

        # pick up the current theme’s logo (make sure each theme folder has this file)
        logo = get_image_path("img/logogg.png", context)

        # resend the main menu
        sent = await context.bot.send_photo(
            chat_id,
            open(logo, "rb"),
            caption="👋 Payment canceled. What next?",
            reply_markup=main_menu_kb()
        )

        # remember this message so delete_old_menu() can remove it later
        context.user_data["menu_msg_id"] = sent.message_id
        return

    # ─── ADMIN CONFIRM / CANCEL / DELIVERED ─────────────────────────────────────
    if a.startswith(("ADMIN_CONFIRM_","ADMIN_CANCEL_","ADMIN_DELIVERED_")):
        cmd, oid = a.split("_")[1], int(a.split("_")[2])
        new_status = {"CONFIRM":"Confirmed","CANCEL":"Cancelled","DELIVERED":"Delivered"}[cmd]
        update_order_status(oid, new_status)
        row = get_order(oid)
        updated = (
            "📦 New Order Received\n"
            f"👤 Name: {row['name']}\n"
            f"☎️ Contact: {row['contact']}\n"
            f"🏠 Address: {row['address']}\n"
            f"📍 Landmark: {row['zip']}\n"
            f"📦 Product: {row['product']}\n"
            f"🔢 Qty: {row['qty']}\n"
            f"💵 Due: PHP {row['total']:.2f}\n"
            f"🚚 Courier: {row['courier']}\n"
            f"⚡️ Delivery: {row['delivery']}\n"
            f"💰 Payment: {row['payment']}\n"
            f"⚪️ Status: {new_status}\n"
            f"🆔 ID: A{oid:06d}"
        )
        return await query.edit_message_text(updated, parse_mode="Markdown", reply_markup=admin_kb_for(oid))

    # ─── ADMIN DELETE ────────────────────────────────────────────────────────────
    if a.startswith("ADMIN_DELETE_") and not a.startswith(("ADMIN_DELETECONFIRM_","ADMIN_DELETEBACK_")):
        oid = int(a.split("_")[2])
        text = query.message.text
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm Delete", callback_data=f"ADMIN_DELETECONFIRM_{oid}")],
            [InlineKeyboardButton("◀️ Back",           callback_data=f"ADMIN_DELETEBACK_{oid}")],
        ])
        return await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    if a.startswith("ADMIN_DELETECONFIRM_"):
        oid = int(a.split("_")[1])
        delete_order(oid)
        return await query.edit_message_text(f"🗑 Order A{oid:06d} has been deleted.", parse_mode="Markdown")

    if a.startswith("ADMIN_DELETEBACK_"):
        oid = int(a.split("_")[2])
        row = get_order(oid)
        if not row:
            return await query.answer("Order not found", show_alert=True)
        updated = (
            "📦 New Order Received\n"
            f"👤 Name: {row['name']}\n"
            f"☎️ Contact: {row['contact']}\n"
            f"🏠 Address: {row['address']}\n"
            f"📍 Landmark: {row['zip']}\n"
            f"📦 Product: {row['product']}\n"
            f"🔢 Qty: {row['qty']}\n"
            f"💵 Due: PHP {row['total']:.2f}\n"
            f"🚚 Courier: {row['courier']}\n"
            f"⚡️ Delivery: {row['delivery']}\n"
            f"💰 Payment: {row['payment']}\n"
            f"⚪️ Status: {row['status']}\n"
            f"🆔 ID: A{oid:06d}"
        )
        return await query.edit_message_text(updated, parse_mode="Markdown", reply_markup=admin_kb_for(oid))

    # ─── MY ORDERS ────────────────────────────────────────────────────────────────
    if a == "MY_ORDER":
        rows = get_user_orders(uid, 1)
        if not rows:
            return await query.message.reply_text("🧾 You have no orders yet.")
        await delete_old_menu()

        # pagination math
        total_orders = count_user_orders(uid)
        total_pages  = max(1, (total_orders + ORDER_PAGE_SIZE - 1) // ORDER_PAGE_SIZE)
        text, kb   = render_orders_page(rows, 1, total_pages)

        # ** new: pick the product image dynamically **
        prod_name = rows[0]["product"]  
        key = next((k for k,v in PRODUCTS.items() if v["name"] == prod_name), None)

        if key:
            img_path = get_image_path(PRODUCTS[key]["image"], context)
            sent = await context.bot.send_photo(
                chat_id,
                photo=open(img_path, "rb"),
                caption=text,
                parse_mode="Markdown",
                reply_markup=kb,
            )
        else:
            # fallback if we can’t find a key
            sent = await context.bot.send_message(
                chat_id, text=text, parse_mode="Markdown", reply_markup=kb
            )

        context.user_data["menu_msg_id"] = sent.message_id
        context.user_data["order_page"]  = 1
        return

    # ─── TRACK ─────────────────────────────────────────────────────────────────────
    if a == "TRACK" and "order_page" in context.user_data:
        rows = get_user_orders(uid, context.user_data["order_page"])
        if not rows:
            return await query.answer()
        order = rows[0]
        if order["tracking"]:
            await query.message.reply_text(f"🚚 Tracking for A{order['id']:06d}: {order['tracking']}")
        else:
            return await query.answer("No tracking available yet.", show_alert=True)
        return

    # ─── PAGINATION ───────────────────────────────────────────────────────────────
    if a.startswith("PAGE_"):
        page = int(a.split("_")[1])

        # ─── user’s own orders ────────────────────────────────────────────
        if "order_page" in context.user_data:
            rows = get_user_orders(uid, page)
            if not rows:
                return await query.answer()
            context.user_data["order_page"] = page
            await query.message.delete()

            total_orders = count_user_orders(uid)
            total_pages  = max(1, (total_orders + ORDER_PAGE_SIZE - 1) // ORDER_PAGE_SIZE)
            text, kb = render_orders_page(rows, page, total_pages)

            # …and here swap send_message for send_photo exactly as above…
            prod_name = rows[0]["product"]
            key = next((k for k,v in PRODUCTS.items() if v["name"] == prod_name), None)
            if key:
                img = get_image_path(PRODUCTS[key]["image"], context)
                sent = await context.bot.send_photo(
                    chat_id, open(img, "rb"),
                    caption=text, parse_mode="Markdown", reply_markup=kb
                )
            else:
                sent = await context.bot.send_message(
                    chat_id, text=text, parse_mode="Markdown", reply_markup=kb
                )

            context.user_data["menu_msg_id"] = sent.message_id
            return

        # ─── *all* orders (admin view) ────────────────────────────────────────────
        rows = get_all_orders(page)
        if not rows:
            return await query.answer()
        await query.message.delete()

        # **compute total pages for ALL orders right here** 
        total_orders_all = count_all_orders()
        total_pages_all  = max(1, (total_orders_all + ORDER_PAGE_SIZE - 1) // ORDER_PAGE_SIZE)

        text, kb = render_orders_page(
            rows,
            page,
            total_pages_all,
            prefix="📋 *All Orders*"
        )
        sent = await context.bot.send_message(
            chat_id, text=text, parse_mode="Markdown", reply_markup=kb
        )
        context.user_data["menu_msg_id"] = sent.message_id
        return

    # ─── FALLBACK ────────────────────────────────────────────────────────────────
    await query.answer()  # no pop-up, no message

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("order_step") != "await_screenshot":
        return
    pid = update.message.photo[-1].file_id
    await context.bot.send_photo(
        ADMIN_GROUP_ID,
        photo=pid,
        caption=context.user_data["admin_text"],
        parse_mode="Markdown"
    )
    qr_id = context.user_data["menu_msg_id"]
    await context.bot.edit_message_reply_markup(
        chat_id=update.effective_chat.id,
        message_id=qr_id,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Paid",    callback_data="PAID"),
            #InlineKeyboardButton("❌ Cancel",  callback_data="CANCEL_PAYMENT"),
            InlineKeyboardButton("☎️ Support",url="https://t.me/")
        ]])
    )
    await update.message.reply_text(
        "✅ Screenshot received! Tap *Paid* next.",
        parse_mode="Markdown"
    )
    context.user_data["order_step"] = "screenshot_received"

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_tracking"):
        oid = context.user_data.pop("awaiting_tracking")
        tn  = update.message.text.strip()
        save_tracking(oid, tn)
        await update.message.reply_text(f"✅ Tracking saved ({tn})")
        ord = get_order(oid)
        await context.bot.send_message(
            ord["user_id"],
            f"🚚 Your order A{oid:06d} tracking #: {tn}"
        )
        return

    step = context.user_data.get("order_step")
    if not step:
        return
    txt = update.message.text.strip()
    if step == "name":
        context.user_data["shipping_name"] = txt
        await update.message.reply_text("Enter your *Address*:", parse_mode="Markdown")
        context.user_data["order_step"] = "address"
        return
    if step == "address":
        context.user_data["shipping_address"] = txt
        await update.message.reply_text("Enter your *Contact Number*:", parse_mode="Markdown")
        context.user_data["order_step"] = "contact"
        return
    if step == "contact":
        context.user_data["shipping_contact"] = txt
        await update.message.reply_text("Enter your *Landmark*:", parse_mode="Markdown")
        context.user_data["order_step"] = "zip"
        return
    if step == "zip":
        context.user_data["shipping_zip"] = txt
        summary_text, summary_kb = build_shipping_summary_and_kb(context.user_data)
        sent = await update.message.reply_text(
            summary_text, parse_mode="Markdown", reply_markup=summary_kb
        )
        context.user_data["menu_msg_id"] = sent.message_id
        context.user_data["order_step"] = "choose"
        return

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "😔 Oops, something went wrong. Please try again later."
        )

# WEBAPP

def admin_auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = None

        # Try to get token from Authorization header or query params
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            if auth_header.startswith('Bearer '):
                token = auth_header[len('Bearer '):]

        if not token:
            token = request.args.get('token')

        if not token:
            return jsonify({"error": "Token missing"}), 401

        try:
            token_data = SIGNER.loads(token, max_age=3600)
            user_id = token_data.get("user_id")

            if user_id not in ADMIN_USER_IDS:
                return jsonify({"error": "Unauthorized"}), 403

            # Optionally, pass user_id to the route function if needed
            return f(*args, **kwargs)

        except SignatureExpired:
            return jsonify({"error": "Token expired"}), 401
        except BadSignature:
            return jsonify({"error": "Invalid token"}), 403

    return decorated_function
 
@web_app.route('/adminpanel', methods=['GET'])
@admin_auth_required
def admin_panel():
    return render_template('admin.html') 

@web_app.route('/products', methods=['GET'])
@admin_auth_required
def list_products():
    try:
        theme = get_global_theme()
        products = []
        for key, p in PRODUCTS.items():
            products.append({
                'key': key,
                'name': p['name'],
                'price': p['price'],
                'image': get_image_path(p['image'], None),
                'stock': get_stock(key)
            })
        return jsonify(products)
    except Exception as e:
        logging.error(f"Error in /products: {e}")
        return jsonify({'error': 'Failed to fetch products'}), 500

@web_app.route('/reset-all-stock', methods=['GET'])
@admin_auth_required
def reset_stock_route():
    try:
        c = DB.cursor()
        c.execute("UPDATE stocks SET stock = 0")
        DB.commit()
        return jsonify({ "success": True })
    except Exception as e:
        logging.error(f"Error in /reset-all-stock: {e}")
        return jsonify({ "error": "Failed to reset stock" }), 500

@web_app.route('/restock-all-products', methods=['POST'])
@admin_auth_required
def add_to_all_products():
    try:
        data = request.get_json(silent=True) or {}
        amt = data.get('stock')
        if not isinstance(amt, int) or isinstance(amt, bool) or amt < 0:
            return jsonify({"error": "Invalid stock value"}), 400
        for k in PRODUCTS:                      # ADD to every product (not overwrite)
            set_stock(k, get_stock(k) + amt)
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"Error in /restock-all-products: {e}")
        return jsonify({"error": "Failed to restock products"}), 500

@web_app.route('/restock-product', methods=['POST'])
@admin_auth_required
def restock_product_route():
    try:
        data = request.get_json(silent=True) or {}
        key, qty = str(data.get('pr_key', '')), data.get('quantity')
        if key not in PRODUCTS:
            return jsonify({"error": "Unknown product"}), 404
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
            return jsonify({"error": "Invalid quantity"}), 400
        set_stock(key, get_stock(key) + qty)
        return jsonify({"success": True})
    except Exception as e:
        logging.error(f"Error in /restock-product: {e}")
        return jsonify({"error": "Failed to restock product"}), 500

def _iso_date(v):
    """DB stores 'Oct 13, 2026'; the panel needs '2026-10-13'."""
    try:
        return datetime.strptime(v, "%b %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        return v

@web_app.route('/orders', methods=['GET'])
@admin_auth_required
def orders_route():
    try:
        status = request.args.get('status', 'all')
        c = DB.cursor()
        if status.lower() != 'all':
            c.execute("SELECT * FROM orders WHERE status = ? ORDER BY id DESC", (status.capitalize(),))
        else:
            c.execute("SELECT * FROM orders ORDER BY id DESC")
        orders = [dict(r) for r in c.fetchall()]
        for o in orders:
            o['created_at'] = _iso_date(o['created_at'])
        return jsonify(orders)
    except Exception as e:
        logging.error(f"Error fetching orders: {e}")
        return jsonify({"error": "Failed to fetch orders"}), 500

@web_app.route('/statistics', methods=['GET'])
@admin_auth_required
def statistics_route():
    try:
        now = datetime.utcnow()

        # Define time ranges
        ranges = {
            'daily': now - timedelta(days=1),
            'weekly': now - timedelta(weeks=1),
            'monthly': now - timedelta(days=30),
            'yearly': now - timedelta(days=365)
        }

        stats = {}
        c = DB.cursor()

        for key, start_time in ranges.items():
            start_time_str = start_time.strftime("%Y-%m-%d %H:%M:%S")

            c.execute("""
                SELECT COUNT(*), SUM(total)
                FROM orders
                WHERE datetime(created_at) >= datetime(?)
            """, (start_time_str,))
            total_orders, total_revenue = c.fetchone()

            total_orders = total_orders or 0
            total_revenue = total_revenue or 0.0
            average_order_value = round(total_revenue / total_orders, 2) if total_orders > 0 else 0.0

            stats[key] = {
                "total_orders": total_orders,
                "total_revenue": round(total_revenue, 2),
                "average_order_value": average_order_value
            }

        # Total products (from PRODUCTS dict)
        total_products = len(PRODUCTS)

        # Unique users count
        c.execute("SELECT COUNT(DISTINCT user_id) FROM orders")
        users_count = c.fetchone()[0] or 0

        # Returning customers (users with more than one order)
        c.execute("""
            SELECT COUNT(*) FROM (
                SELECT user_id
                FROM orders
                GROUP BY user_id
                HAVING COUNT(*) > 1
            )
        """)
        returning_customers = c.fetchone()[0] or 0

        # Users data – latest order per user (assuming most recent is useful)
        c.execute("""
            SELECT o1.user_id, o1.name, o1.contact, o1.address, o1.zip, MAX(o1.created_at) as last_order
            FROM orders o1
            GROUP BY o1.user_id
        """)
        users_data = [{
            "user_id": row["user_id"],
            "name": row["name"],
            "contact": row["contact"],
            "address": row["address"],
            "zip": row["zip"],
            "last_order": row["last_order"]
        } for row in c.fetchall()]

        return jsonify({
            "statistics": stats,
            "total_products": total_products,
            "users_count": users_count,
            "returning_customers": returning_customers,
            "users": users_data
        })

    except Exception as e:
        import logging
        logging.error(f"Error in /statistics: {e}")
        return jsonify({"error": "Failed to compute statistics"}), 500


# ═══ >>> ADMIN PANEL: NEW ENDPOINTS ═══════════════════════════════════════════
VALID_STATUSES = ("Pending", "Confirmed", "Delivered", "Cancelled")
QR_PATH = os.path.abspath(os.path.join("img", "QR", "gcash.png"))   # same file the bot uses

def tg_send(chat_id, text) -> bool:
    """Send a Telegram message from the Flask thread (plain HTTP, no event loop needed)."""
    try:
        body = urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=body)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        logging.warning(f"tg_send to {chat_id} failed: {e}")
        return False

@web_app.route('/update-order', methods=['POST'])
@admin_auth_required
def update_order_route():
    data = request.get_json(silent=True) or {}
    try:
        oid = int(data.get('id'))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid order id"}), 400
    row = get_order(oid)
    if not row:
        return jsonify({"error": "Order not found"}), 404

    status = data.get('status')
    if status is not None:
        status = str(status).capitalize()
        if status not in VALID_STATUSES:
            return jsonify({"error": "Invalid status"}), 400
        if status != row["status"]:
            update_order_status(oid, status)

    tracking = data.get('tracking')
    if tracking is not None:
        tracking = str(tracking).strip()
        if tracking != (row["tracking"] or ""):
            save_tracking(oid, tracking)
            if tracking:   # same message the bot sends when you type a tracking number
                tg_send(row["user_id"], f"🚚 Your order A{oid:06d} tracking #: {tracking}")
    return jsonify({"success": True})

@web_app.route('/update-price', methods=['POST'])
@admin_auth_required
def update_price_route():
    data = request.get_json(silent=True) or {}
    key = str(data.get('key', ''))
    if key not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 404
    try:
        price = float(data.get('price'))
        if price < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid price"}), 400
    PRODUCTS[key]["price"] = price
    c = DB.cursor()
    c.execute("UPDATE products SET price=? WHERE key=?", (price, key))
    DB.commit()
    return jsonify({"success": True})

@web_app.route('/add-product', methods=['POST'])
@admin_auth_required
def add_product_route():
    data = request.get_json(silent=True) or {}
    key, name = str(data.get('key', '')).strip(), str(data.get('name', '')).strip()
    if not re.fullmatch(r"[a-z0-9-]{1,30}", key):
        return jsonify({"error": "ID must be lowercase letters, numbers or dashes (max 30)."}), 400
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if key in PRODUCTS:
        return jsonify({"error": "That ID already exists"}), 400
    try:
        price = float(data.get('price'))
        stock = int(data.get('stock') or 0)
        if price < 0 or stock < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid price or stock"}), 400
    image = f"img/product/{key}.png"
    c = DB.cursor()
    c.execute("INSERT INTO products VALUES (?,?,?,?,?)", (key, name, price, 1, image))
    DB.commit()
    PRODUCTS[key] = {"name": name, "price": price, "min_qty": 1, "image": image}
    set_stock(key, stock)
    return jsonify({"success": True})

@web_app.route('/delete-product', methods=['POST'])
@admin_auth_required
def delete_product_route():
    data = request.get_json(silent=True) or {}
    key = str(data.get('key', ''))
    if key not in PRODUCTS:
        return jsonify({"error": "Unknown product"}), 404
    PRODUCTS.pop(key, None)
    c = DB.cursor()
    c.execute("DELETE FROM products WHERE key=?", (key,))
    c.execute("DELETE FROM stocks WHERE product_key=?", (key,))
    DB.commit()
    return jsonify({"success": True})

def _theme_names():
    d = os.path.join("img", "themes")
    try:
        return sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))
    except FileNotFoundError:
        return []

@web_app.route('/themes', methods=['GET'])
@admin_auth_required
def themes_route():
    return jsonify({"themes": _theme_names(), "current": get_global_theme()})

@web_app.route('/set-theme', methods=['POST'])
@admin_auth_required
def set_theme_route():
    theme = str((request.get_json(silent=True) or {}).get('theme', ''))
    if theme not in _theme_names():
        return jsonify({"error": "Unknown theme"}), 400
    set_global_theme(theme)
    return jsonify({"success": True})

def _img_kind(head: bytes):
    if head.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png"
    if head.startswith(b"\xff\xd8\xff"):      return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP": return "image/webp"
    return None

@web_app.route('/qr', methods=['GET'])
@admin_auth_required
def qr_route():
    if not os.path.exists(QR_PATH):
        return jsonify({"error": "No QR found"}), 404
    with open(QR_PATH, "rb") as f:
        mime = _img_kind(f.read(16)) or "image/png"
    resp = send_file(QR_PATH, mimetype=mime)
    resp.headers["Cache-Control"] = "no-store"
    return resp

@web_app.route('/set-qr', methods=['POST'])
@admin_auth_required
def set_qr_route():
    f = request.files.get('qr')
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    blob = f.read(5 * 1024 * 1024 + 1)
    if len(blob) > 5 * 1024 * 1024:
        return jsonify({"error": "Image is over 5 MB"}), 400
    if not _img_kind(blob[:16]):
        return jsonify({"error": "Only PNG, JPG or WEBP images"}), 400
    os.makedirs(os.path.dirname(QR_PATH), exist_ok=True)
    if os.path.exists(QR_PATH):                       # keep a backup of the old QR
        os.replace(QR_PATH, QR_PATH.replace("gcash.png", "gcash_old.png"))
    with open(QR_PATH, "wb") as out:
        out.write(blob)
    return jsonify({"success": True})

@web_app.route('/broadcast', methods=['POST'])
@admin_auth_required
def broadcast_route():
    msg = str((request.get_json(silent=True) or {}).get('message', '')).strip()
    if not msg or len(msg) > 4000:
        return jsonify({"error": "Message must be 1-4000 characters"}), 400
    c = DB.cursor()
    c.execute("SELECT user_id FROM referrals UNION SELECT user_id FROM orders WHERE user_id IS NOT NULL")
    ids = [r[0] for r in c.fetchall()]
    sent = 0
    for uid in ids:
        if tg_send(uid, msg):
            sent += 1
        time.sleep(0.05)          # stay under Telegram's ~30 msg/sec limit
    return jsonify({"success": True, "sent": sent, "total": len(ids)})

@web_app.route('/settings', methods=['GET', 'POST'])
@admin_auth_required
def settings_route():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        if 'maintenance' in data:
            set_setting("maintenance", "1" if data['maintenance'] else "0")
    return jsonify({"maintenance": is_maintenance()})
# ═══ <<< ADMIN PANEL: END NEW ENDPOINTS ═════════════════════════════════════════

@web_app.route('/auth', methods=['POST'])
def auth_route():
    data = request.json

    if not data or 'token' not in data:
        return jsonify({"success": False, "error": "Token missing"}), 400

    token = data['token']

    try:
        # Validate and decode the token
        token_data = SIGNER.loads(token, max_age=3600)  # Token expires in 1 hour
        user_id = token_data.get("user_id")

        # Check if user is allowed
        if user_id not in ADMIN_USER_IDS:
            return jsonify({"success": False, "error": "Unauthorized"}), 403

        return jsonify({"success": True, "user_id": user_id})

    except SignatureExpired:
        return jsonify({"success": False, "error": "Token expired"}), 401

    except BadSignature:
        return jsonify({"success": False, "error": "Invalid token"}), 403

def run_flask():
    logging.info("Flask app is running")
    web_app.run(host="127.0.0.1", port=5001, debug=False, use_reloader=False)

def main():
    # Start Flask app in a background thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    # Run Telegram bot in the main thread
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Add your Telegram handlers
    app.add_handler(CommandHandler("admindashboard",         admindashboard_handler))
    app.add_handler(CommandHandler("stocklist",              stocklist_handler))
    app.add_handler(CommandHandler("productnames",           productnames_handler))
    app.add_handler(CommandHandler("restock",                restock_handler))
    app.add_handler(CommandHandler("admincommands",          admincommands_handler))
    app.add_handler(CommandHandler("orders",                 orders_handler))
    app.add_handler(CommandHandler("order",                  order_handler))
    app.add_handler(CommandHandler("user_orders",            user_orders_handler))
    app.add_handler(CommandHandler("orders_date",            orders_date_handler))
    app.add_handler(CommandHandler("stats",                  stats_handler))
    app.add_handler(CommandHandler("top_products",           top_products_handler))
    app.add_handler(CommandHandler("low_stock",              low_stock_handler))
    app.add_handler(CommandHandler("export_orders",          export_orders_handler))
    app.add_handler(CommandHandler("bulk_update_status",     bulk_update_status_handler))
    app.add_handler(CommandHandler("stock_alerts",           stock_alerts_handler))
    app.add_handler(CommandHandler("set_warning_threshold",  set_warning_threshold_handler))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^!"), bang_handler))

    app.add_handler(CommandHandler("set_theme", set_theme_command))
    app.add_handler(CommandHandler("stocklist_reset", stocklist_reset_handler))
    app.add_handler(CommandHandler("orderlist", orderlist_handler))
    app.add_handler(CommandHandler("stock_all", stock_all_handler))

    # Core handlers
    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CallbackQueryHandler(menu_handler))
    app.add_handler(MessageHandler(filters.PHOTO,    photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Error handler
    app.add_error_handler(error_handler)

    logging.info("Telegram bot is running")
    app.run_polling()  # Run this in the main thread only


if __name__ == "__main__":
    main()
