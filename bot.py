import os
import io
import qrcode
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
BOT_TOKEN        = os.getenv('BOT_TOKEN')
MONGO_URI        = os.getenv('MONGO_URI')
ADMIN_ID         = int(os.getenv('ADMIN_ID'))
CONTACT_USERNAME = os.getenv('CONTACT_USERNAME', 'admin')

bot    = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db     = client['sub_management']

channels_col  = db['channels']
users_col     = db['users']
admin_qr_col  = db['admin_qr']
user_qr_col   = db['user_qr_timers']
settings_col  = db['settings']          # UPI ID, welcome message, etc.
broadcast_col = db['broadcast_state']   # Broadcast state tracking

# ==============================================================
# HELPERS
# ==============================================================

def make_label(mins_str):
    m = int(mins_str)
    if m < 60:
        return f"{m} Min"
    elif m < 1440:
        return f"{m//60} Hours"
    else:
        return f"{m//1440} Days"

def get_setting(key, default=None):
    doc = settings_col.find_one({"key": key})
    return doc['value'] if doc else default

def set_setting(key, value):
    settings_col.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)

def get_upi_id():
    return get_setting("upi_id", os.getenv('UPI_ID', ''))

def get_welcome_message():
    return get_setting(
        "welcome_message",
        "🌟 *Welcome to Our Premium Channel Bot!*\n\n"
        "Yahan aap premium channels ka subscription le sakte hain.\n\n"
        "✅ Instant Access\n"
        "💳 UPI Payment\n"
        "🔒 Secure & Trusted\n\n"
        "Neeche diye gaye link se apna channel choose karein! 👇"
    )

def get_force_channels():
    doc = settings_col.find_one({"key": "force_channels"})
    return doc['value'] if doc else []

def register_user(user_id):
    users_col.update_one(
        {"user_id": user_id, "type": "bot_user"},
        {"$setOnInsert": {"joined_at": datetime.now()}},
        upsert=True
    )

def is_subscribed_all(user_id):
    """Check if user is subscribed to all force-join channels."""
    force_chs = get_force_channels()
    for ch_id in force_chs:
        try:
            member = bot.get_chat_member(ch_id, user_id)
            if member.status in ['left', 'kicked', 'banned']:
                return False, ch_id
        except:
            return False, ch_id
    return True, None

def generate_upi_qr(upi_id, amount):
    """Generate UPI QR code image bytes."""
    upi_string = f"upi://pay?pa={upi_id}&am={amount}&cu=INR"
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(upi_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# ==============================================================
# FORCE SUBSCRIBE CHECK
# ==============================================================

def check_force_subscribe(message_or_call):
    """Returns True if user can proceed, False if blocked."""
    if hasattr(message_or_call, 'from_user'):
        user = message_or_call.from_user
        chat_id = message_or_call.message.chat.id if hasattr(message_or_call, 'message') else message_or_call.chat.id
    else:
        user = message_or_call.from_user
        chat_id = message_or_call.chat.id

    force_chs = get_force_channels()
    if not force_chs:
        return True

    ok, blocked_ch_id = is_subscribed_all(user.id)
    if ok:
        return True

    # Build join buttons
    markup = InlineKeyboardMarkup()
    for ch_id in force_chs:
        try:
            ch_info = bot.get_chat(ch_id)
            invite = bot.export_chat_invite_link(ch_id)
            markup.add(InlineKeyboardButton(f"📢 Join {ch_info.title}", url=invite))
        except:
            pass
    markup.add(InlineKeyboardButton("✅ Maine Join Kar Liya", callback_data="check_joined"))

    bot.send_message(
        chat_id,
        "⚠️ *Pehle in channels ko join karein!*\n\n"
        "Bot use karne ke liye in channels ka member hona zaroori hai.\n\n"
        "Join karne ke baad '✅ Maine Join Kar Liya' click karein.",
        reply_markup=markup, parse_mode="Markdown"
    )
    return False

@bot.callback_query_handler(func=lambda call: call.data == "check_joined")
def cb_check_joined(call):
    bot.answer_callback_query(call.id)
    ok, _ = is_subscribed_all(call.from_user.id)
    if ok:
        bot.edit_message_text(
            "✅ *Verified! Ab aap bot use kar sakte hain.*\n\n/start karein.",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown"
        )
    else:
        bot.answer_callback_query(call.id, "❌ Abhi bhi kuch channels join nahi hain!", show_alert=True)

# ==============================================================
# /start
# ==============================================================

@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id
    register_user(user_id)
    parts   = message.text.split()

    # Force subscribe check (except for admin)
    if user_id != ADMIN_ID:
        if not check_force_subscribe(message):
            return

    if len(parts) > 1 and user_id != ADMIN_ID:
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

                # Send channel photo if available
                ch_photo = ch_data.get('photo_file_id')
                welcome_text = (
                    f"🎉 *{ch_data['name']}* mein Welcome!\n\n"
                    f"{ch_data.get('description', 'Premium content ke liye subscribe karein.')}\n\n"
                    f"💳 *Subscription Plans:*"
                )
                if ch_photo:
                    bot.send_photo(
                        message.chat.id, ch_photo,
                        caption=welcome_text,
                        reply_markup=markup, parse_mode="Markdown"
                    )
                else:
                    bot.send_message(
                        message.chat.id, welcome_text,
                        reply_markup=markup, parse_mode="Markdown"
                    )
                return
        except:
            pass

    if user_id == ADMIN_ID:
        send_admin_panel(message.chat.id)
    else:
        # Show welcome message with optional photo
        welcome_photo = get_setting("welcome_photo")
        welcome_text  = get_welcome_message()
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📢 Channels Dekhein", callback_data="browse_channels"))
        markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))

        if welcome_photo:
            bot.send_photo(
                message.chat.id, welcome_photo,
                caption=welcome_text,
                reply_markup=markup, parse_mode="Markdown"
            )
        else:
            bot.send_message(
                message.chat.id, welcome_text,
                reply_markup=markup, parse_mode="Markdown"
            )

def send_admin_panel(chat_id, msg_id=None):
    upi = get_upi_id() or "❌ Set Nahi Hua"
    force_chs = get_force_channels()
    text = (
        "🛠️ *Admin Control Panel*\n\n"
        f"🏦 Current UPI ID: `{upi}`\n"
        f"🔒 Force Subscribe Channels: {len(force_chs)}\n\n"
        "📋 *Commands:*\n"
        "/add — Channel Add/Edit\n"
        "/channels — Channels Manage\n"
        "/uploadqr — QR Upload\n"
        "/setupi — UPI ID Set Karo\n"
        "/setwelcome — Welcome Message Edit\n"
        "/setwelcomephoto — Welcome Photo Set\n"
        "/broadcast — Broadcast Message\n"
        "/forcesub — Force Subscribe Channels Manage\n"
        "/stats — Bot Statistics"
    )
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("➕ Channel Add", callback_data="add_new"),
        InlineKeyboardButton("📢 Channels", callback_data="list_channels_cb")
    )
    markup.row(
        InlineKeyboardButton("🏦 UPI Set", callback_data="setupi_cb"),
        InlineKeyboardButton("📣 Broadcast", callback_data="broadcast_cb")
    )
    markup.row(
        InlineKeyboardButton("🔒 Force Sub", callback_data="forcesub_cb"),
        InlineKeyboardButton("✏️ Welcome Edit", callback_data="editwelcome_cb")
    )
    markup.add(InlineKeyboardButton("📊 Stats", callback_data="stats_cb"))

    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, parse_mode="Markdown", reply_markup=markup)
        except:
            bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

# ==============================================================
# UPI ID MANAGEMENT
# ==============================================================

@bot.message_handler(commands=['setupi'], func=lambda m: m.from_user.id == ADMIN_ID)
def setupi_cmd(message):
    msg = bot.send_message(
        ADMIN_ID,
        "🏦 *UPI ID Set Karo*\n\nApna UPI ID type karein:\n\nExample: `yourname@paytm`",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, save_upi_id)

@bot.callback_query_handler(func=lambda call: call.data == "setupi_cb" and call.from_user.id == ADMIN_ID)
def setupi_cb(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        ADMIN_ID,
        "🏦 *UPI ID Set Karo*\n\nApna UPI ID type karein:\n\nExample: `yourname@paytm`",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, save_upi_id)

def save_upi_id(message):
    upi = message.text.strip()
    if '@' not in upi:
        bot.send_message(ADMIN_ID, "❌ Invalid UPI ID! `name@bank` format mein hona chahiye.")
        return
    set_setting("upi_id", upi)
    bot.send_message(
        ADMIN_ID,
        f"✅ *UPI ID Save Ho Gaya!*\n\n`{upi}`\n\nAb se users ko yeh UPI ID dikhega.",
        parse_mode="Markdown"
    )

# ==============================================================
# WELCOME MESSAGE MANAGEMENT
# ==============================================================

@bot.message_handler(commands=['setwelcome'], func=lambda m: m.from_user.id == ADMIN_ID)
def setwelcome_cmd(message):
    _ask_welcome_message(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "editwelcome_cb" and call.from_user.id == ADMIN_ID)
def editwelcome_cb(call):
    bot.answer_callback_query(call.id)
    _ask_welcome_message(call.message.chat.id)

def _ask_welcome_message(chat_id):
    current = get_welcome_message()
    msg = bot.send_message(
        chat_id,
        f"✏️ *Welcome Message Edit*\n\nCurrent message:\n\n{current}\n\n"
        "Naya welcome message type karein:\n_(Markdown supported: *bold*, _italic_, `code`)_",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, save_welcome_message)

def save_welcome_message(message):
    set_setting("welcome_message", message.text)
    bot.send_message(ADMIN_ID, "✅ *Welcome message update ho gaya!*", parse_mode="Markdown")

@bot.message_handler(commands=['setwelcomephoto'], func=lambda m: m.from_user.id == ADMIN_ID)
def setwelcomephoto_cmd(message):
    msg = bot.send_message(ADMIN_ID, "📸 Welcome ke liye photo bhejein (ya /skip karein hataane ke liye):")
    bot.register_next_step_handler(msg, save_welcome_photo)

def save_welcome_photo(message):
    if message.text and message.text.strip() == '/skip':
        set_setting("welcome_photo", None)
        bot.send_message(ADMIN_ID, "✅ Welcome photo hata diya gaya.")
    elif message.photo:
        set_setting("welcome_photo", message.photo[-1].file_id)
        bot.send_photo(ADMIN_ID, message.photo[-1].file_id,
                       caption="✅ *Welcome photo save ho gaya!*", parse_mode="Markdown")
    else:
        bot.send_message(ADMIN_ID, "❌ Photo nahi mila. Dobara try karein /setwelcomephoto")

# ==============================================================
# FORCE SUBSCRIBE MANAGEMENT
# ==============================================================

@bot.message_handler(commands=['forcesub'], func=lambda m: m.from_user.id == ADMIN_ID)
def forcesub_cmd(message):
    _show_forcesub_panel(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "forcesub_cb" and call.from_user.id == ADMIN_ID)
def forcesub_cb(call):
    bot.answer_callback_query(call.id)
    _show_forcesub_panel(call.message.chat.id)

def _show_forcesub_panel(chat_id):
    force_chs = get_force_channels()
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("➕ Channel Add Karo", callback_data="forcesub_add"))

    text = "🔒 *Force Subscribe Channels*\n\n"
    if force_chs:
        for ch_id in force_chs:
            try:
                ch_info = bot.get_chat(ch_id)
                name = ch_info.title
            except:
                name = str(ch_id)
            markup.add(InlineKeyboardButton(f"🗑️ Remove: {name}", callback_data=f"forcesub_remove_{ch_id}"))
        text += f"Total: {len(force_chs)} channels\n\nRemove karne ke liye button dabayein:"
    else:
        text += "Koi force subscribe channel nahi hai."

    bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "forcesub_add" and call.from_user.id == ADMIN_ID)
def forcesub_add_cb(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        ADMIN_ID,
        "📢 *Force Subscribe Channel Add*\n\n"
        "Us channel se koi bhi message *forward* karein jise aap force subscribe mein add karna chahte hain.\n\n"
        "⚠️ Bot us channel ka admin hona chahiye.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, save_forcesub_channel)

def save_forcesub_channel(message):
    if message.forward_from_chat:
        ch_id = message.forward_from_chat.id
        force_chs = get_force_channels()
        if ch_id not in force_chs:
            force_chs.append(ch_id)
            set_setting("force_channels", force_chs)
            bot.send_message(
                ADMIN_ID,
                f"✅ *Channel add ho gaya!*\n\n`{message.forward_from_chat.title}` ({ch_id})\n\n"
                f"Total force channels: {len(force_chs)}",
                parse_mode="Markdown"
            )
        else:
            bot.send_message(ADMIN_ID, "ℹ️ Yeh channel pehle se add hai.")
    else:
        bot.send_message(ADMIN_ID, "❌ Channel message forward nahi hua. Dobara try karein /forcesub")

@bot.callback_query_handler(func=lambda call: call.data.startswith("forcesub_remove_") and call.from_user.id == ADMIN_ID)
def forcesub_remove_cb(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split("forcesub_remove_")[1])
    force_chs = get_force_channels()
    if ch_id in force_chs:
        force_chs.remove(ch_id)
        set_setting("force_channels", force_chs)
        bot.send_message(ADMIN_ID, f"✅ Channel remove ho gaya. Remaining: {len(force_chs)}")
    else:
        bot.send_message(ADMIN_ID, "❌ Channel nahi mila.")

# ==============================================================
# BROADCAST
# ==============================================================

@bot.message_handler(commands=['broadcast'], func=lambda m: m.from_user.id == ADMIN_ID)
def broadcast_cmd(message):
    _start_broadcast(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "broadcast_cb" and call.from_user.id == ADMIN_ID)
def broadcast_cb(call):
    bot.answer_callback_query(call.id)
    _start_broadcast(call.message.chat.id)

def _start_broadcast(chat_id):
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("📝 Text Message", callback_data="bc_type_text"),
        InlineKeyboardButton("📸 Photo + Text", callback_data="bc_type_photo")
    )
    markup.add(InlineKeyboardButton("🎥 Video + Text", callback_data="bc_type_video"))
    bot.send_message(
        chat_id,
        "📣 *Broadcast Message Bhejo*\n\nKis type ka message bhejana hai?",
        reply_markup=markup, parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("bc_type_") and call.from_user.id == ADMIN_ID)
def bc_type_select(call):
    bot.answer_callback_query(call.id)
    bc_type = call.data.split("bc_type_")[1]
    broadcast_col.update_one(
        {"admin_id": ADMIN_ID},
        {"$set": {"type": bc_type, "status": "awaiting_content"}},
        upsert=True
    )
    if bc_type == "text":
        msg = bot.send_message(ADMIN_ID, "✏️ Broadcast message type karein:")
    elif bc_type == "photo":
        msg = bot.send_message(ADMIN_ID, "📸 Photo bhejein (caption ke saath ya bina):")
    elif bc_type == "video":
        msg = bot.send_message(ADMIN_ID, "🎥 Video bhejein (caption ke saath ya bina):")
    bot.register_next_step_handler(msg, receive_broadcast_content)

def receive_broadcast_content(message):
    bc_state = broadcast_col.find_one({"admin_id": ADMIN_ID})
    if not bc_state:
        return

    bc_type = bc_state.get("type", "text")

    # Count total users
    total_users = users_col.count_documents({"type": "bot_user"})

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Send Karo", callback_data="bc_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel")
    )

    # Store message details
    if bc_type == "text" and message.text:
        broadcast_col.update_one(
            {"admin_id": ADMIN_ID},
            {"$set": {"content_type": "text", "text": message.text, "status": "preview"}}
        )
        bot.send_message(
            ADMIN_ID,
            f"📋 *Preview:*\n\n{message.text}\n\n"
            f"👥 Total recipients: *{total_users}* users\n\nSend karein?",
            reply_markup=markup, parse_mode="Markdown"
        )

    elif message.photo:
        broadcast_col.update_one(
            {"admin_id": ADMIN_ID},
            {"$set": {"content_type": "photo", "file_id": message.photo[-1].file_id,
                      "caption": message.caption or "", "status": "preview"}}
        )
        bot.send_photo(
            ADMIN_ID, message.photo[-1].file_id,
            caption=f"{message.caption or ''}\n\n━━━━━━━━━━━━\n"
                    f"📋 Preview | 👥 Recipients: *{total_users}*\n\nSend karein?",
            reply_markup=markup, parse_mode="Markdown"
        )

    elif message.video:
        broadcast_col.update_one(
            {"admin_id": ADMIN_ID},
            {"$set": {"content_type": "video", "file_id": message.video.file_id,
                      "caption": message.caption or "", "status": "preview"}}
        )
        bot.send_video(
            ADMIN_ID, message.video.file_id,
            caption=f"{message.caption or ''}\n\n━━━━━━━━━━━━\n"
                    f"📋 Preview | 👥 Recipients: *{total_users}*\n\nSend karein?",
            reply_markup=markup, parse_mode="Markdown"
        )
    else:
        bot.send_message(ADMIN_ID, "❌ Content nahi mila. Dobara try karein.")

@bot.callback_query_handler(func=lambda call: call.data == "bc_confirm" and call.from_user.id == ADMIN_ID)
def bc_confirm(call):
    bot.answer_callback_query(call.id)
    bc_state = broadcast_col.find_one({"admin_id": ADMIN_ID})
    if not bc_state:
        bot.send_message(ADMIN_ID, "❌ Broadcast state nahi mila.")
        return

    bot.edit_message_text(
        "📤 *Broadcasting shuru ho raha hai...*",
        call.message.chat.id, call.message.message_id, parse_mode="Markdown"
    ) if bc_state.get('content_type') == 'text' else None

    all_users = users_col.find({"type": "bot_user"})
    sent = 0
    failed = 0

    for user in all_users:
        uid = user['user_id']
        if uid == ADMIN_ID:
            continue
        try:
            ctype = bc_state.get('content_type', 'text')
            if ctype == 'text':
                bot.send_message(uid, bc_state['text'], parse_mode="Markdown")
            elif ctype == 'photo':
                bot.send_photo(uid, bc_state['file_id'], caption=bc_state.get('caption', ''), parse_mode="Markdown")
            elif ctype == 'video':
                bot.send_video(uid, bc_state['file_id'], caption=bc_state.get('caption', ''), parse_mode="Markdown")
            sent += 1
            time.sleep(0.05)  # Flood control
        except:
            failed += 1

    broadcast_col.delete_one({"admin_id": ADMIN_ID})
    bot.send_message(
        ADMIN_ID,
        f"✅ *Broadcast Complete!*\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"📊 Total: {sent + failed}",
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data == "bc_cancel" and call.from_user.id == ADMIN_ID)
def bc_cancel(call):
    bot.answer_callback_query(call.id)
    broadcast_col.delete_one({"admin_id": ADMIN_ID})
    bot.edit_message_text("❌ Broadcast cancel kar diya.", call.message.chat.id, call.message.message_id)

# ==============================================================
# STATS
# ==============================================================

@bot.message_handler(commands=['stats'], func=lambda m: m.from_user.id == ADMIN_ID)
def stats_cmd(message):
    _show_stats(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "stats_cb" and call.from_user.id == ADMIN_ID)
def stats_cb(call):
    bot.answer_callback_query(call.id)
    _show_stats(call.message.chat.id)

def _show_stats(chat_id):
    total_users    = users_col.count_documents({"type": "bot_user"})
    total_channels = channels_col.count_documents({"admin_id": ADMIN_ID})
    active_subs    = users_col.count_documents({"expiry": {"$gte": datetime.now().timestamp()}})
    upi            = get_upi_id() or "Set nahi hua"

    bot.send_message(
        chat_id,
        f"📊 *Bot Statistics*\n\n"
        f"👥 Total Users: {total_users}\n"
        f"📢 Total Channels: {total_channels}\n"
        f"✅ Active Subscribers: {active_subs}\n"
        f"🏦 UPI ID: `{upi}`",
        parse_mode="Markdown"
    )

# ==============================================================
# ADMIN: /channels
# ==============================================================

@bot.message_handler(commands=['channels'], func=lambda m: m.from_user.id == ADMIN_ID)
def list_channels(message):
    _show_channels_list(message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "list_channels_cb" and call.from_user.id == ADMIN_ID)
def list_channels_cb(call):
    bot.answer_callback_query(call.id)
    _show_channels_list(call.message.chat.id)

def _show_channels_list(chat_id):
    markup = InlineKeyboardMarkup()
    count  = 0
    for ch in channels_col.find({"admin_id": ADMIN_ID}):
        markup.add(InlineKeyboardButton(f"📢 {ch['name']}", callback_data=f"manage_{ch['channel_id']}"))
        count += 1
    markup.add(InlineKeyboardButton("➕ Add New Channel", callback_data="add_new"))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data="back_admin"))
    text = "Your Managed Channels:" if count else "No channels found. Click below to add one."
    bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "back_admin" and call.from_user.id == ADMIN_ID)
def back_admin_cb(call):
    bot.answer_callback_query(call.id)
    send_admin_panel(call.message.chat.id, call.message.message_id)

# ==============================================================
# ADMIN: /add  — with photo support
# ==============================================================

@bot.message_handler(commands=['add'], func=lambda m: m.from_user.id == ADMIN_ID)
def add_channel_start(message):
    msg = bot.send_message(ADMIN_ID, "Channel ka koi bhi message *FORWARD* karein yahan:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, get_plans)

@bot.callback_query_handler(func=lambda call: call.data == "add_new" and call.from_user.id == ADMIN_ID)
def cb_add_new(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(ADMIN_ID, "Channel ka koi bhi message *FORWARD* karein yahan:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, get_plans)

def get_plans(message):
    if message.forward_from_chat:
        ch_id   = message.forward_from_chat.id
        ch_name = message.forward_from_chat.title
        msg = bot.send_message(
            ADMIN_ID,
            f"✅ Channel: *{ch_name}*\n\n"
            "Plans enter karein `Minutes:Price` format mein:\n\n"
            "Example: `1440:99, 43200:199`\n_(1 Day = ₹99, 30 Days = ₹199)_",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, get_description, ch_id, ch_name)
    else:
        bot.send_message(ADMIN_ID, "❌ Channel se forward nahi hua. /add se dobara try karein.")

def get_description(message, ch_id, ch_name):
    try:
        plans_dict = {}
        for p in message.text.split(','):
            t, pr = p.strip().split(':')
            plans_dict[t.strip()] = pr.strip()

        msg = bot.send_message(
            ADMIN_ID,
            "📝 Channel ka description enter karein (ya /skip karein):",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, get_channel_photo, ch_id, ch_name, plans_dict)
    except:
        bot.send_message(ADMIN_ID, "❌ Invalid format. `Min:Price, Min:Price` format mein likhein.")

def get_channel_photo(message, ch_id, ch_name, plans_dict):
    description = "" if (message.text and message.text.strip() == '/skip') else (message.text or "")
    msg = bot.send_message(
        ADMIN_ID,
        "📸 Channel ki photo/thumbnail bhejein (ya /skip karein):\n\n"
        "_(Yeh photo users ko channel page par dikhegi)_",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, finalize_channel, ch_id, ch_name, plans_dict, description)

def finalize_channel(message, ch_id, ch_name, plans_dict, description):
    photo_file_id = None
    if message.photo:
        photo_file_id = message.photo[-1].file_id
    # Skip if text message
    update_data = {
        "name": ch_name,
        "plans": plans_dict,
        "admin_id": ADMIN_ID,
        "description": description
    }
    if photo_file_id:
        update_data["photo_file_id"] = photo_file_id

    channels_col.update_one(
        {"channel_id": ch_id},
        {"$set": update_data},
        upsert=True
    )
    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start={ch_id}"

    reply_text = (
        f"✅ *Channel Setup Complete!*\n\n"
        f"📢 Channel: {ch_name}\n"
        f"🔗 Invite Link:\n`{link}`\n\n"
        f"Share this link with customers!"
    )
    if photo_file_id:
        bot.send_photo(ADMIN_ID, photo_file_id, caption=reply_text, parse_mode="Markdown")
    else:
        bot.send_message(ADMIN_ID, reply_text, parse_mode="Markdown")

# ==============================================================
# ADMIN: /uploadqr
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
    bot.send_message(ADMIN_ID, "📤 *Channel select karein (QR upload ke liye):*", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('qrch_') and call.from_user.id == ADMIN_ID)
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
        f"📢 *{ch_data['name']}*\n\nPlan select karein:",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('qrplan_') and call.from_user.id == ADMIN_ID)
def qr_select_plan(call):
    bot.answer_callback_query(call.id)
    _, ch_id_s, mins = call.data.split('_')
    ch_id   = int(ch_id_s)
    ch_data = channels_col.find_one({"channel_id": ch_id})
    price   = ch_data['plans'][mins]

    admin_qr_col.update_one(
        {"admin_id": ADMIN_ID, "status": "awaiting"},
        {"$set": {"ch_id": ch_id, "mins": mins, "price": price,
                  "status": "awaiting", "created_at": datetime.now()}},
        upsert=True
    )
    label = make_label(mins)
    bot.edit_message_text(
        f"✅ Plan: *{label} — ₹{price}*\n\n"
        f"📸 Ab QR code ka *image* bhejein.\n\n"
        f"💡 Ya /autogenqr type karein auto UPI QR generate karne ke liye.",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['autogenqr'], func=lambda m: m.from_user.id == ADMIN_ID)
def auto_gen_qr(message):
    """Auto-generate UPI QR for pending plan."""
    pending = admin_qr_col.find_one({"admin_id": ADMIN_ID, "status": "awaiting"})
    if not pending:
        bot.send_message(ADMIN_ID, "❌ Pehle /uploadqr se plan select karein.")
        return
    upi = get_upi_id()
    if not upi:
        bot.send_message(ADMIN_ID, "❌ UPI ID set nahi hai. Pehle /setupi use karein.")
        return

    price   = pending['price']
    ch_id   = pending['ch_id']
    mins    = pending['mins']
    ch_data = channels_col.find_one({"channel_id": ch_id})
    label   = make_label(mins)

    buf = generate_upi_qr(upi, price)
    admin_qr_col.update_one(
        {"admin_id": ADMIN_ID, "ch_id": ch_id, "mins": mins},
        {"$set": {"qr_type": "auto", "price": price, "status": "active", "updated_at": datetime.now()}},
        upsert=True
    )
    admin_qr_col.delete_one({"admin_id": ADMIN_ID, "status": "awaiting"})

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔄 Replace QR", callback_data=f"qrch_{ch_id}"))
    bot.send_photo(
        ADMIN_ID, buf,
        caption=(
            f"✅ *Auto UPI QR Generate Ho Gaya!*\n\n"
            f"📢 Channel: {ch_data['name']}\n"
            f"💳 Plan: {label} — ₹{price}\n"
            f"🏦 UPI: `{upi}`\n\n"
            f"Users ko yeh automatically generate hoga har baar."
        ),
        reply_markup=markup, parse_mode="Markdown"
    )

@bot.message_handler(content_types=['photo'], func=lambda m: m.from_user.id == ADMIN_ID)
def receive_admin_qr(message):
    """Admin sends QR photo — save permanently for that channel+plan."""
    pending = admin_qr_col.find_one({"admin_id": ADMIN_ID, "status": "awaiting"})
    if not pending:
        return

    ch_id   = pending['ch_id']
    mins    = pending['mins']
    price   = pending['price']
    file_id = message.photo[-1].file_id
    ch_data = channels_col.find_one({"channel_id": ch_id})
    label   = make_label(mins)

    admin_qr_col.update_one(
        {"admin_id": ADMIN_ID, "ch_id": ch_id, "mins": mins},
        {"$set": {
            "file_id":    file_id,
            "qr_type":    "manual",
            "price":      price,
            "status":     "active",
            "updated_at": datetime.now()
        }},
        upsert=True
    )
    admin_qr_col.delete_one({"admin_id": ADMIN_ID, "status": "awaiting"})

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🔄 Replace This QR", callback_data=f"qrch_{ch_id}"))
    bot.send_photo(
        ADMIN_ID, file_id,
        caption=(
            f"✅ *QR Saved!*\n\n"
            f"📢 Channel: {ch_data['name']}\n"
            f"💳 Plan: {label} — ₹{price}\n\n"
            f"⏱️ Each user gets *5 minutes* to pay."
        ),
        reply_markup=markup, parse_mode="Markdown"
    )

# ==============================================================
# USER: Browse Channels
# ==============================================================

@bot.callback_query_handler(func=lambda call: call.data == "browse_channels")
def browse_channels(call):
    if call.from_user.id != ADMIN_ID:
        if not check_force_subscribe(call):
            return
    bot.answer_callback_query(call.id)
    markup = InlineKeyboardMarkup()
    count  = 0
    for ch in channels_col.find({"admin_id": ADMIN_ID}):
        markup.add(InlineKeyboardButton(f"📢 {ch['name']}", callback_data=f"viewch_{ch['channel_id']}"))
        count += 1
    if count == 0:
        bot.send_message(call.message.chat.id, "Abhi koi channel available nahi hai.")
        return
    bot.send_message(call.message.chat.id, "📢 *Available Channels:*\n\nKoi bhi channel select karein:", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('viewch_'))
def view_channel(call):
    if call.from_user.id != ADMIN_ID:
        if not check_force_subscribe(call):
            return
    bot.answer_callback_query(call.id)
    ch_id   = int(call.data.split('viewch_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        bot.send_message(call.message.chat.id, "❌ Channel nahi mila.")
        return
    markup = InlineKeyboardMarkup()
    for p_time, p_price in ch_data['plans'].items():
        markup.add(InlineKeyboardButton(
            f"💳 {make_label(p_time)} — ₹{p_price}",
            callback_data=f"select_{ch_id}_{p_time}"
        ))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    welcome_text = (
        f"🎉 *{ch_data['name']}*\n\n"
        f"{ch_data.get('description', 'Premium channel')}\n\n"
        f"💳 *Plans:*"
    )
    ch_photo = ch_data.get('photo_file_id')
    if ch_photo:
        bot.send_photo(call.message.chat.id, ch_photo, caption=welcome_text, reply_markup=markup, parse_mode="Markdown")
    else:
        bot.send_message(call.message.chat.id, welcome_text, reply_markup=markup, parse_mode="Markdown")

# ==============================================================
# USER: plan select → show QR → start 5-min user timer
# ==============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('select_'))
def user_selects_plan(call):
    if call.from_user.id != ADMIN_ID:
        if not check_force_subscribe(call):
            return
    _, ch_id_s, mins = call.data.split('_')
    ch_id   = int(ch_id_s)
    user_id = call.from_user.id
    ch_data = channels_col.find_one({"channel_id": ch_id})
    price   = ch_data['plans'][mins]
    label   = make_label(mins)
    upi     = get_upi_id()

    qr_doc = admin_qr_col.find_one({"admin_id": ADMIN_ID, "ch_id": ch_id, "mins": mins, "status": "active"})

    user_expiry_ts = int((datetime.now() + timedelta(minutes=5)).timestamp())

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Maine Payment Kar Di", callback_data=f"paid_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("📞 Contact Admin",        url=f"https://t.me/{CONTACT_USERNAME}"))

    caption = (
        f"📸 *Payment QR Code*\n\n"
        f"💳 Plan: {label}\n"
        f"💰 Amount: ₹{price}\n"
        f"🏦 UPI ID: `{upi}`\n\n"
        f"⏱️ *Yeh QR 5 minute mein expire hoga!*\n"
        f"Jaldi payment karein aur neeche click karein."
    )

    if qr_doc and qr_doc.get('qr_type') == 'manual' and qr_doc.get('file_id'):
        sent = bot.send_photo(call.message.chat.id, qr_doc['file_id'], caption=caption, reply_markup=markup, parse_mode="Markdown")
    else:
        # Auto-generate UPI QR using qrcode library
        if upi:
            buf = generate_upi_qr(upi, price)
            sent = bot.send_photo(call.message.chat.id, buf, caption=caption, reply_markup=markup, parse_mode="Markdown")
        else:
            # Fallback to API
            qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=upi://pay?pa={upi}%26am={price}%26cu=INR"
            sent = bot.send_photo(call.message.chat.id, qr_url, caption=caption, reply_markup=markup, parse_mode="Markdown")

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

    t = Thread(
        target=_user_qr_timer,
        args=(user_id, ch_id, mins, price, label, sent.message_id, user_expiry_ts),
        daemon=True
    )
    t.start()


def _user_qr_timer(user_id, ch_id, mins, price, label, msg_id, expiry_ts):
    upi = get_upi_id()
    markup_paying = InlineKeyboardMarkup()
    markup_paying.add(InlineKeyboardButton("✅ Maine Payment Kar Di", callback_data=f"paid_{ch_id}_{mins}"))
    markup_paying.add(InlineKeyboardButton("📞 Contact Admin",        url=f"https://t.me/{CONTACT_USERNAME}"))

    while True:
        time.sleep(60)
        remaining = int(expiry_ts - datetime.now().timestamp())

        record = user_qr_col.find_one({"user_id": user_id, "ch_id": ch_id, "mins": mins})
        if record and record.get('status') == 'paid':
            return

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
                    f"🏦 UPI ID: `{upi}`\n\n"
                    f"⏱️ *QR expires in: {rm}m {rs}s*\n"
                    f"Jaldi payment karein!"
                ),
                chat_id=user_id, message_id=msg_id,
                reply_markup=markup_paying, parse_mode="Markdown"
            )
        except:
            pass

    record = user_qr_col.find_one({"user_id": user_id, "ch_id": ch_id, "mins": mins})
    if record and record.get('status') == 'paid':
        return

    user_qr_col.update_one(
        {"user_id": user_id, "ch_id": ch_id, "mins": mins},
        {"$set": {"status": "expired"}}
    )

    bot_username = bot.get_me().username
    markup_expired = InlineKeyboardMarkup()
    markup_expired.add(InlineKeyboardButton("🔄 Dobara Try Karein", url=f"https://t.me/{bot_username}?start={ch_id}"))
    markup_expired.add(InlineKeyboardButton("📞 Contact Admin",     url=f"https://t.me/{CONTACT_USERNAME}"))

    try:
        bot.edit_message_caption(
            caption=(
                "⚠️ *QR Code Expire Ho Gaya!*\n\n"
                "Aapka 5 minute ka payment window khatam ho gaya.\n\n"
                "Dobara try karne ke liye button dabayein."
            ),
            chat_id=user_id, message_id=msg_id,
            reply_markup=markup_expired, parse_mode="Markdown"
        )
    except:
        pass

    try:
        bot.send_message(
            user_id,
            "⏰ *Payment Time Expire!*\n\n"
            "Aapne 5 minute mein payment nahi ki, QR expire ho gaya.\n\n"
            "Agar payment ki hai toh admin se contact karein.\n"
            "Dobara try karne ke liye button dabayein. 👇",
            reply_markup=markup_expired, parse_mode="Markdown"
        )
    except:
        pass

# ==============================================================
# USER: "I Have Paid"
# ==============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('paid_'))
def user_paid_notify(call):
    _, ch_id_s, mins = call.data.split('_')
    ch_id   = int(ch_id_s)
    user    = call.from_user
    ch_data = channels_col.find_one({"channel_id": ch_id})
    price   = ch_data['plans'][mins]
    label   = make_label(mins)

    record = user_qr_col.find_one({"user_id": user.id, "ch_id": ch_id, "mins": mins})
    if record and record.get('status') == 'expired':
        bot_username = bot.get_me().username
        bot.answer_callback_query(call.id, "⚠️ QR expire ho chuka hai! Dobara try karein.", show_alert=True)
        return

    user_qr_col.update_one(
        {"user_id": user.id, "ch_id": ch_id, "mins": mins},
        {"$set": {"status": "paid"}}
    )

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Approve", callback_data=f"app_{user.id}_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("❌ Reject",  callback_data=f"rej_{user.id}_{ch_id}"))

    bot.send_message(
        ADMIN_ID,
        f"🔔 *Payment Verification Required!*\n\n"
        f"👤 User: {user.first_name} (@{user.username or 'N/A'}) (ID: `{user.id}`)\n"
        f"📢 Channel: {ch_data['name']}\n"
        f"💳 Plan: {label}\n"
        f"💰 Price: ₹{price}",
        reply_markup=markup, parse_mode="Markdown"
    )

    u_markup = InlineKeyboardMarkup()
    u_markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    bot.send_message(
        call.message.chat.id,
        "✅ *Payment request bhej di gayi!*\n\nAdmin se approval ka wait karein. ⏳",
        reply_markup=u_markup, parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, "✅ Request bhej di gayi!")

# ==============================================================
# ADMIN: Approve / Reject
# ==============================================================

@bot.callback_query_handler(func=lambda call: call.data.startswith('app_') and call.from_user.id == ADMIN_ID)
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
            {"user_id": u_id, "channel_id": ch_id, "type": "subscriber"},
            {"$set": {"expiry": expiry_dt.timestamp()}},
            upsert=True
        )

        bot.send_message(
            u_id,
            f"🥳 *Payment Approved!*\n\n"
            f"💳 Plan: {label}\n"
            f"🔗 Join Link: {link.invite_link}\n\n"
            f"⚠️ Access {label} mein expire hoga.",
            parse_mode="Markdown"
        )
        bot.edit_message_text(
            f"✅ Approved: User {u_id} — {label}",
            call.message.chat.id, call.message.message_id
        )
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Error: {e}")


@bot.callback_query_handler(func=lambda call: call.data.startswith('rej_') and call.from_user.id == ADMIN_ID)
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

@bot.callback_query_handler(func=lambda call: call.data.startswith('manage_') and call.from_user.id == ADMIN_ID)
def manage_ch(call):
    ch_id    = int(call.data.split('manage_')[1])
    ch_data  = channels_col.find_one({"channel_id": ch_id})
    bot_user = bot.get_me().username
    link     = f"https://t.me/{bot_user}?start={ch_id}"

    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("📸 Photo Change", callback_data=f"chphoto_{ch_id}"),
        InlineKeyboardButton("💳 Plans Edit", callback_data=f"editplans_{ch_id}")
    )
    markup.add(InlineKeyboardButton("🗑️ Delete Channel", callback_data=f"delch_{ch_id}"))
    markup.add(InlineKeyboardButton("🔙 Back", callback_data="list_channels_cb"))

    text = (
        f"⚙️ *{ch_data['name']}*\n\n"
        f"🔗 Invite Link:\n`{link}`\n\n"
        f"📝 Description: {ch_data.get('description', 'N/A')}\n"
        f"📸 Photo: {'✅' if ch_data.get('photo_file_id') else '❌'}\n\n"
        f"💳 *Plans:*\n" +
        "\n".join([f"• {make_label(t)}: ₹{p}" for t, p in ch_data['plans'].items()])
    )

    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('chphoto_') and call.from_user.id == ADMIN_ID)
def change_ch_photo(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('chphoto_')[1])
    msg = bot.send_message(ADMIN_ID, "📸 Naya photo bhejein (ya /skip karein hataane ke liye):")
    bot.register_next_step_handler(msg, save_ch_photo, ch_id)

def save_ch_photo(message, ch_id):
    if message.text and message.text.strip() == '/skip':
        channels_col.update_one({"channel_id": ch_id}, {"$unset": {"photo_file_id": ""}})
        bot.send_message(ADMIN_ID, "✅ Channel photo hata diya.")
    elif message.photo:
        channels_col.update_one({"channel_id": ch_id}, {"$set": {"photo_file_id": message.photo[-1].file_id}})
        bot.send_photo(ADMIN_ID, message.photo[-1].file_id, caption="✅ Channel photo update ho gaya!", parse_mode="Markdown")
    else:
        bot.send_message(ADMIN_ID, "❌ Photo nahi mila.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('delch_') and call.from_user.id == ADMIN_ID)
def delete_channel(call):
    ch_id = int(call.data.split('delch_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Haan, Delete Karo", callback_data=f"confirmdelch_{ch_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="list_channels_cb")
    )
    bot.edit_message_text(
        f"⚠️ *{ch_data['name']}* ko delete karna chahte hain?\n\nYeh action undo nahi ho sakta!",
        call.message.chat.id, call.message.message_id,
        parse_mode="Markdown", reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirmdelch_') and call.from_user.id == ADMIN_ID)
def confirm_delete_channel(call):
    ch_id = int(call.data.split('confirmdelch_')[1])
    channels_col.delete_one({"channel_id": ch_id})
    admin_qr_col.delete_many({"ch_id": ch_id})
    bot.edit_message_text("✅ Channel delete ho gaya.", call.message.chat.id, call.message.message_id)

# ==============================================================
# SCHEDULED JOBS
# ==============================================================

def kick_expired_users():
    now      = datetime.now().timestamp()
    bot_user = bot.get_me().username
    for user in users_col.find({"expiry": {"$lte": now}, "type": "subscriber"}):
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
    cutoff = (datetime.now() - timedelta(hours=1)).timestamp()
    user_qr_col.delete_many({"status": {"$in": ["expired", "paid"]}, "created_at": {"$lte": cutoff}})

# ==============================================================
# STARTUP
# ==============================================================

if __name__ == '__main__':
    keep_alive()
    scheduler = BackgroundScheduler()
    scheduler.add_job(kick_expired_users,  'interval', minutes=1)
    scheduler.add_job(cleanup_old_user_qr, 'interval', minutes=30)
    scheduler.start()
    bot.remove_webhook()
    print("✅ Enhanced Bot is running...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
