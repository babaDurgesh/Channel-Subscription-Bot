import os
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from threading import Thread
import time

# --- RENDER KEEP-ALIVE SERVER ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is running and healthy!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run_web, daemon=True).start()

# --- CONFIGURATION (Environment Variables) ---
BOT_TOKEN       = os.getenv('BOT_TOKEN')
MONGO_URI       = os.getenv('MONGO_URI')
ADMIN_ID        = int(os.getenv('ADMIN_ID'))
UPI_ID          = os.getenv('UPI_ID')
CONTACT_USERNAME = os.getenv('CONTACT_USERNAME')

bot    = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db     = client['sub_management']

channels_col  = db['channels']
users_col     = db['users']
admin_qr_col  = db['admin_qr']       # Stores admin's uploaded QR (permanent until replaced)
user_qr_col   = db['user_qr_timers'] # Per-user 5-min QR expiry sessions

# ==============================================================
# HELPER
# ==============================================================

def make_label(mins_str):
    m = int(mins_str)
    if m < 60:
        return f"{m} Min"
    elif m < 1440:
        return f"{m//60} Hours"
    else:
        return f"{m//1440} Days"

# ==============================================================
# /start
# ==============================================================

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    parts   = message.text.split()

    if len(parts) > 1:
        try:
            ch_id   = int(parts[1])
            ch_data = channels_col.find_one({"channel_id": ch_id})
            if ch_data:
                markup = InlineKeyboardMarkup()
                for p_time, p_price in ch_data['plans'].items():
                    markup.add(InlineKeyboardButton(
                        f"💳 {make_label(p_time)} — ₹{p_price}",
                        callback_data=f"select_{ch_id}_{p_time}"
                    ))
                markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
                bot.send_message(
                    message.chat.id,
                    f"Welcome!\n\nYou are joining: *{ch_data['name']}*\n\nPlease select a subscription plan:",
                    reply_markup=markup, parse_mode="Markdown"
                )
                return
        except:
            pass

    if user_id == ADMIN_ID:
        bot.send_message(
            message.chat.id,
            "✅ *Admin Panel Active!*\n\n"
            "/add — Add/Edit Channel & Prices\n"
            "/channels — Manage Existing Channels\n"
            "/uploadqr — Upload Payment QR Code (permanent until replaced)",
            parse_mode="Markdown"
        )
    else:
        bot.send_message(message.chat.id, "Welcome! To join a channel, please use the link provided by the Admin.")

# ==============================================================
# ADMIN: /channels
# ==============================================================

@bot.message_handler(commands=['channels'], func=lambda m: m.from_user.id == ADMIN_ID)
def list_channels(message):
    markup = InlineKeyboardMarkup()
    count  = 0
    for ch in channels_col.find({"admin_id": ADMIN_ID}):
        markup.add(InlineKeyboardButton(f"📢 {ch['name']}", callback_data=f"manage_{ch['channel_id']}"))
        count += 1
    markup.add(InlineKeyboardButton("➕ Add New Channel", callback_data="add_new"))
    text = "Your Managed Channels:" if count else "No channels found. Click below to add one."
    bot.send_message(ADMIN_ID, text, reply_markup=markup)

# ==============================================================
# ADMIN: /add
# ==============================================================

@bot.message_handler(commands=['add'], func=lambda m: m.from_user.id == ADMIN_ID)
def add_channel_start(message):
    msg = bot.send_message(ADMIN_ID, "Ensure the bot is Admin in your channel, then *FORWARD* any message from that channel here.", parse_mode="Markdown")
    bot.register_next_step_handler(msg, get_plans)

@bot.callback_query_handler(func=lambda call: call.data == "add_new")
def cb_add_new(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(ADMIN_ID, "Please *FORWARD* any message from your channel here.", parse_mode="Markdown")
    bot.register_next_step_handler(msg, get_plans)

def get_plans(message):
    if message.forward_from_chat:
        ch_id   = message.forward_from_chat.id
        ch_name = message.forward_from_chat.title
        msg = bot.send_message(
            ADMIN_ID,
            f"Channel Detected: *{ch_name}*\n\n"
            "Enter plans in format `Minutes:Price, Minutes:Price`\n\n"
            "Example: `1440:99, 43200:199`\n(1 Day and 30 Days)",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, finalize_channel, ch_id, ch_name)
    else:
        bot.send_message(ADMIN_ID, "❌ Message was not forwarded. Use /add to try again.")

def finalize_channel(message, ch_id, ch_name):
    try:
        plans_dict = {}
        for p in message.text.split(','):
            t, pr = p.strip().split(':')
            plans_dict[t.strip()] = pr.strip()
        channels_col.update_one(
            {"channel_id": ch_id},
            {"$set": {"name": ch_name, "plans": plans_dict, "admin_id": ADMIN_ID}},
            upsert=True
        )
        bot_username = bot.get_me().username
        bot.send_message(
            ADMIN_ID,
            f"✅ Setup Successful!\n\nInvite Link:\n`https://t.me/{bot_username}?start={ch_id}`",
            parse_mode="Markdown"
        )
    except:
        bot.send_message(ADMIN_ID, "❌ Invalid format. Use `Min:Price, Min:Price`. Try /add again.")

# ==============================================================
# ADMIN: /uploadqr  — upload permanent QR (per channel+plan)
# ==============================================================

@bot.message_handler(commands=['uploadqr'], func=lambda m: m.from_user.id == ADMIN_ID)
def upload_qr_start(message):
    markup = InlineKeyboardMarkup()
    count  = 0
    for ch in channels_col.find({"admin_id": ADMIN_ID}):
        markup.add(InlineKeyboardButton(f"📢 {ch['name']}", callback_data=f"qrch_{ch['channel_id']}"))
        count += 1
    if count == 0:
        bot.send_message(ADMIN_ID, "❌ No channels found. Use /add first.")
        return
    bot.send_message(ADMIN_ID, "📤 *Select the channel to upload QR for:*", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('qrch_'))
def qr_select_channel(call):
    bot.answer_callback_query(call.id)
    ch_id   = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        bot.send_message(ADMIN_ID, "❌ Channel not found.")
        return
    markup = InlineKeyboardMarkup()
    for p_time, p_price in ch_data['plans'].items():
        markup.add(InlineKeyboardButton(
            f"💳 {make_label(p_time)} — ₹{p_price}",
            callback_data=f"qrplan_{ch_id}_{p_time}"
        ))
    bot.edit_message_text(
        f"📢 *{ch_data['name']}*\n\nSelect the plan for this QR:",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('qrplan_'))
def qr_select_plan(call):
    bot.answer_callback_query(call.id)
    _, ch_id_s, mins = call.data.split('_')
    ch_id   = int(ch_id_s)
    ch_data = channels_col.find_one({"channel_id": ch_id})
    price   = ch_data['plans'][mins]

    # Save pending state (which channel+plan admin will upload QR for)
    admin_qr_col.update_one(
        {"admin_id": ADMIN_ID, "status": "awaiting"},
        {"$set": {"ch_id": ch_id, "mins": mins, "price": price,
                  "status": "awaiting", "created_at": datetime.now()}},
        upsert=True
    )
    label = make_label(mins)
    bot.edit_message_text(
        f"✅ Plan: *{label} — ₹{price}*\n\n"
        f"📸 Now send the QR code *image*.\n\n"
        f"ℹ️ This QR will be shown to users permanently (until you upload a new one).\n"
        f"Users will each get *5 minutes* to pay after they see the QR.",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown"
    )

@bot.message_handler(content_types=['photo'], func=lambda m: m.from_user.id == ADMIN_ID)
def receive_admin_qr(message):
    """Admin sends QR photo — save it permanently for that channel+plan."""
    pending = admin_qr_col.find_one({"admin_id": ADMIN_ID, "status": "awaiting"})
    if not pending:
        return  # not in upload flow

    ch_id   = pending['ch_id']
    mins    = pending['mins']
    price   = pending['price']
    file_id = message.photo[-1].file_id
    ch_data = channels_col.find_one({"channel_id": ch_id})
    label   = make_label(mins)

    # Save QR permanently (upsert per channel+plan)
    admin_qr_col.update_one(
        {"admin_id": ADMIN_ID, "ch_id": ch_id, "mins": mins},
        {"$set": {
            "file_id":    file_id,
            "price":      price,
            "status":     "active",
            "updated_at": datetime.now()
        }},
        upsert=True
    )
    # Remove awaiting state
    admin_qr_col.delete_one({"admin_id": ADMIN_ID, "status": "awaiting"})

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔄 Replace This QR", callback_data=f"qrch_{ch_id}"))

    bot.send_photo(
        ADMIN_ID, file_id,
        caption=(
            f"✅ *QR Saved Successfully!*\n\n"
            f"📢 Channel: {ch_data['name']}\n"
            f"💳 Plan: {label} — ₹{price}\n\n"
            f"👤 Users will see this QR when they select this plan.\n"
            f"⏱️ Each user gets *5 minutes* to pay after seeing it.\n"
            f"If they don't pay in time, the QR expires *for them only*."
        ),
        reply_markup=markup, parse_mode="Markdown"
    )

# ==============================================================
# USER: plan select → show QR → start 5-min user timer
# ==============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('select_'))
def user_selects_plan(call):
    _, ch_id_s, mins = call.data.split('_')
    ch_id   = int(ch_id_s)
    user_id = call.from_user.id
    ch_data = channels_col.find_one({"channel_id": ch_id})
    price   = ch_data['plans'][mins]
    label   = make_label(mins)

    # Check if admin has a QR uploaded for this channel+plan
    qr_doc = admin_qr_col.find_one({"admin_id": ADMIN_ID, "ch_id": ch_id, "mins": mins, "status": "active"})

    # 5-min expiry FOR THIS USER from now
    user_expiry_ts = int((datetime.now() + timedelta(minutes=5)).timestamp())

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Maine Payment Kar Di", callback_data=f"paid_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("📞 Contact Admin",        url=f"https://t.me/{CONTACT_USERNAME}"))

    if qr_doc:
        # Admin has custom QR — show it, start user timer
        sent = bot.send_photo(
            call.message.chat.id,
            qr_doc['file_id'],
            caption=(
                f"📸 *Payment QR Code*\n\n"
                f"💳 Plan: {label}\n"
                f"💰 Amount: ₹{price}\n"
                f"🏦 UPI ID: `{UPI_ID}`\n\n"
                f"⏱️ *Yeh QR aapke liye 5 minute mein expire ho jayega!*\n"
                f"Jaldi payment karein aur neeche click karein."
            ),
            reply_markup=markup, parse_mode="Markdown"
        )
    else:
        # Fallback: auto-generate UPI QR
        qr_url = (
            f"https://api.qrserver.com/v1/create-qr-code/?size=300x300"
            f"&data=upi://pay?pa={UPI_ID}%26am={price}%26cu=INR"
        )
        sent = bot.send_photo(
            call.message.chat.id,
            qr_url,
            caption=(
                f"📸 *Payment QR Code*\n\n"
                f"💳 Plan: {label}\n"
                f"💰 Amount: ₹{price}\n"
                f"🏦 UPI ID: `{UPI_ID}`\n\n"
                f"⏱️ *Yeh QR aapke liye 5 minute mein expire ho jayega!*\n"
                f"Jaldi payment karein aur neeche click karein."
            ),
            reply_markup=markup, parse_mode="Markdown"
        )

    # Save user QR timer in DB
    user_qr_col.update_one(
        {"user_id": user_id, "ch_id": ch_id, "mins": mins},
        {"$set": {
            "expiry_ts":  user_expiry_ts,
            "msg_id":     sent.message_id,
            "price":      price,
            "status":     "active",
            "created_at": datetime.now()
        }},
        upsert=True
    )

    # Background thread: update caption every 60s + notify on expiry
    t = Thread(
        target=_user_qr_timer,
        args=(user_id, ch_id, mins, price, label, sent.message_id, user_expiry_ts),
        daemon=True
    )
    t.start()


def _user_qr_timer(user_id, ch_id, mins, price, label, msg_id, expiry_ts):
    """
    Runs in background FOR EACH USER.
    Updates countdown in caption every 60 sec.
    When 5 min is over → edit caption to EXPIRED + notify.
    """
    markup_paying = InlineKeyboardMarkup()
    markup_paying.add(InlineKeyboardButton("✅ Maine Payment Kar Di", callback_data=f"paid_{ch_id}_{mins}"))
    markup_paying.add(InlineKeyboardButton("📞 Contact Admin",        url=f"https://t.me/{CONTACT_USERNAME}"))

    # Update every 60 seconds until expiry
    while True:
        time.sleep(60)
        remaining = int(expiry_ts - datetime.now().timestamp())

        # Check if user already paid (status set to 'paid' by admin_notify)
        record = user_qr_col.find_one({"user_id": user_id, "ch_id": ch_id, "mins": mins})
        if record and record.get('status') == 'paid':
            return  # User paid — stop timer silently

        if remaining <= 0:
            break

        rm = remaining // 60
        rs = remaining % 60
        try:
            bot.edit_message_caption(
                caption=(
                    f"📸 *Payment QR Code*\n\n"
                    f"💳 Plan: {label}\n"
                    f"💰 Amount: ₹{price}\n"
                    f"🏦 UPI ID: `{UPI_ID}`\n\n"
                    f"⏱️ *QR expires in: {rm}m {rs}s*\n"
                    f"Jaldi payment karein!"
                ),
                chat_id=user_id, message_id=msg_id,
                reply_markup=markup_paying, parse_mode="Markdown"
            )
        except:
            pass

    # Check again before expiring — maybe they paid in last 60 sec
    record = user_qr_col.find_one({"user_id": user_id, "ch_id": ch_id, "mins": mins})
    if record and record.get('status') == 'paid':
        return

    # ---- QR EXPIRED for this user ----
    user_qr_col.update_one(
        {"user_id": user_id, "ch_id": ch_id, "mins": mins},
        {"$set": {"status": "expired"}}
    )

    bot_username = bot.get_me().username
    markup_expired = InlineKeyboardMarkup()
    markup_expired.add(InlineKeyboardButton("🔄 Dobara Try Karein", url=f"https://t.me/{bot_username}?start={ch_id}"))
    markup_expired.add(InlineKeyboardButton("📞 Contact Admin",     url=f"https://t.me/{CONTACT_USERNAME}"))

    # Edit original QR message
    try:
        bot.edit_message_caption(
            caption=(
                "⚠️ *QR Code Expire Ho Gaya!*\n\n"
                "Aapka 5 minute ka payment window khatam ho gaya.\n\n"
                "Dobara try karne ke liye neeche button dabayein."
            ),
            chat_id=user_id, message_id=msg_id,
            reply_markup=markup_expired, parse_mode="Markdown"
        )
    except:
        pass

    # Send a fresh notification message
    try:
        bot.send_message(
            user_id,
            "⏰ *Payment Time Expire!*\n\n"
            "Aapne 5 minute mein payment nahi ki, isliye QR code expire ho gaya.\n\n"
            "Agar payment ki hai toh admin se contact karein.\n"
            "Dobara try karne ke liye button dabayein. 👇",
            reply_markup=markup_expired, parse_mode="Markdown"
        )
    except:
        pass


# ==============================================================
# USER: "I Have Paid" button
# ==============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('paid_'))
def user_paid_notify(call):
    _, ch_id_s, mins = call.data.split('_')
    ch_id   = int(ch_id_s)
    user    = call.from_user
    ch_data = channels_col.find_one({"channel_id": ch_id})
    price   = ch_data['plans'][mins]
    label   = make_label(mins)

    # Check if QR already expired for this user
    record = user_qr_col.find_one({"user_id": user.id, "ch_id": ch_id, "mins": mins})
    if record and record.get('status') == 'expired':
        bot_username = bot.get_me().username
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🔄 Dobara Try Karein", url=f"https://t.me/{bot_username}?start={ch_id}"))
        bot.answer_callback_query(call.id, "⚠️ QR expire ho chuka hai! Dobara try karein.", show_alert=True)
        return

    # Mark as paid (stop timer thread)
    user_qr_col.update_one(
        {"user_id": user.id, "ch_id": ch_id, "mins": mins},
        {"$set": {"status": "paid"}}
    )

    # Notify admin for approval
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"app_{user.id}_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("❌ Reject",  callback_data=f"rej_{user.id}_{ch_id}"))

    bot.send_message(
        ADMIN_ID,
        f"🔔 *Payment Verification Required!*\n\n"
        f"👤 User: {user.first_name} (ID: {user.id})\n"
        f"📢 Channel: {ch_data['name']}\n"
        f"💳 Plan: {label}\n"
        f"💰 Price: ₹{price}",
        reply_markup=markup, parse_mode="Markdown"
    )

    u_markup = InlineKeyboardMarkup()
    u_markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    bot.send_message(
        call.message.chat.id,
        "✅ *Payment request bhej di gayi!*\n\nAdmin se approval ka wait karein.",
        reply_markup=u_markup, parse_mode="Markdown"
    )

# ==============================================================
# ADMIN: Approve / Reject
# ==============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('app_'))
def approve_now(call):
    parts    = call.data.split('_')
    u_id     = int(parts[1])
    ch_id    = int(parts[2])
    mins     = parts[3]
    mins_int = int(mins)

    try:
        expiry_dt = datetime.now() + timedelta(minutes=mins_int)
        expiry_ts = int(expiry_dt.timestamp())
        link      = bot.create_chat_invite_link(ch_id, member_limit=1, expire_date=expiry_ts)
        label     = make_label(mins)

        users_col.update_one(
            {"user_id": u_id, "channel_id": ch_id},
            {"$set": {"expiry": expiry_dt.timestamp()}},
            upsert=True
        )

        bot.send_message(
            u_id,
            f"🥳 *Payment Approved!*\n\n"
            f"💳 Plan: {label}\n"
            f"🔗 Join Link: {link.invite_link}\n\n"
            f"⚠️ Aapka access {label} mein expire ho jayega.",
            parse_mode="Markdown"
        )
        bot.edit_message_text(
            f"✅ Approved: User {u_id} — {label}",
            call.message.chat.id, call.message.message_id
        )
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Error: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('rej_'))
def reject_payment(call):
    parts = call.data.split('_')
    u_id  = int(parts[1])
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    bot.send_message(
        u_id,
        "❌ *Payment verify nahi hua.*\n\nAdmin se contact karein.",
        reply_markup=markup, parse_mode="Markdown"
    )
    bot.edit_message_text(
        f"❌ Rejected: User {u_id}",
        call.message.chat.id, call.message.message_id
    )

# ==============================================================
# ADMIN: manage channel
# ==============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('manage_'))
def manage_ch(call):
    ch_id    = int(call.data.split('_')[1])
    ch_data  = channels_col.find_one({"channel_id": ch_id})
    bot_user = bot.get_me().username
    link     = f"https://t.me/{bot_user}?start={ch_id}"
    bot.edit_message_text(
        f"⚙️ *{ch_data['name']}*\n\n"
        f"Invite Link: `{link}`\n\n"
        f"Prices change karne ke liye /add use karein aur channel ka message forward karein.",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown"
    )

# ==============================================================
# SCHEDULED JOBS
# ==============================================================

def kick_expired_users():
    """Remove users whose channel subscription has ended."""
    now      = datetime.now().timestamp()
    bot_user = bot.get_me().username
    for user in users_col.find({"expiry": {"$lte": now}}):
        try:
            bot.ban_chat_member(user['channel_id'], user['user_id'])
            bot.unban_chat_member(user['channel_id'], user['user_id'])
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔄 Renew", url=f"https://t.me/{bot_user}?start={user['channel_id']}"))
            bot.send_message(
                user['user_id'],
                "⚠️ *Aapka subscription expire ho gaya.*\n\nRenew karne ke liye button dabayein.",
                reply_markup=markup, parse_mode="Markdown"
            )
            users_col.delete_one({"_id": user['_id']})
        except:
            pass

def cleanup_old_user_qr():
    """Clean up very old expired user QR records (older than 1 hour)."""
    cutoff = (datetime.now() - timedelta(hours=1)).timestamp()
    user_qr_col.delete_many({"status": {"$in": ["expired", "paid"]}, "created_at": {"$lte": cutoff}})

# ==============================================================
# STARTUP
# ==============================================================

if __name__ == '__main__':
    keep_alive()
    scheduler = BackgroundScheduler()
    scheduler.add_job(kick_expired_users,   'interval', minutes=1)
    scheduler.add_job(cleanup_old_user_qr,  'interval', minutes=30)
    scheduler.start()
    bot.remove_webhook()
    print("Bot is running...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
