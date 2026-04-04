import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading, sys

# ── ENV ─────────────────────────────────────────────
BOT_TOKEN  = os.environ.get("ADMIN_BOT_TOKEN")
ADMIN_ID   = os.environ.get("ADMIN_ID")
BASE_URL   = os.environ.get("BASE_URL")
YANDEX_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FID = os.environ.get("YANDEX_FOLDER_ID")
GOOGLE_SHEET_URL = os.environ.get("GOOGLE_SHEET_URL")

if not BOT_TOKEN or not ADMIN_ID or not BASE_URL:
    print("❌ Проверь ENV переменные")
    sys.exit()

ADMIN_ID = int(ADMIN_ID)

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ── DB ─────────────────────────────────────────────
DATA_FILE = "db.json"
_lock = threading.Lock()

def load_data():
    if os.path.exists(DATA_FILE):
        return json.load(open(DATA_FILE, "r", encoding="utf-8"))
    return {"products": {}, "crm": {}}

def save_data():
    with _lock:
        json.dump({"products": products, "crm": crm}, open(DATA_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

data = load_data()
products = data["products"]
crm = data["crm"]

user_state = {}

# ── GOOGLE SHEETS ──────────────────────────────────
def save_to_sheet(user_id, name, username, status, message):
    if not GOOGLE_SHEET_URL:
        return
    try:
        requests.post(GOOGLE_SHEET_URL, json={
            "user_id": user_id,
            "name": name,
            "username": username,
            "status": status,
            "message": message
        }, timeout=5)
    except:
        pass

# ── CRM ────────────────────────────────────────────
def save_lead(msg, status):
    uid = str(msg.chat.id)
    name = msg.from_user.first_name or "Без имени"
    uname = msg.from_user.username or ""

    crm[uid] = {
        "name": name,
        "username": uname,
        "status": status,
        "last": msg.text
    }

    save_data()

    # 🔥 В Google Sheets
    save_to_sheet(uid, name, uname, status, msg.text)

def notify_admin(msg):
    bot.send_message(
        ADMIN_ID,
        f"🔥 ГОРЯЧИЙ ЛИД\n\n"
        f"{msg.from_user.first_name}\n"
        f"@{msg.from_user.username}\n"
        f"{msg.text}\n\n"
        f"tg://user?id={msg.chat.id}"
    )

# ── UI ─────────────────────────────────────────────
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Каталог", "Помощь", "Контакты")
    return kb

def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Добавить товар", "Лиды", "Рассылка")
    return kb

# ── START ──────────────────────────────────────────
@bot.message_handler(commands=["start"])
def start(msg):
    if msg.chat.id == ADMIN_ID:
        bot.send_message(msg.chat.id, "👑 Админ панель", reply_markup=admin_menu())
    else:
        bot.send_message(msg.chat.id, "Привет! Напиши что ищешь", reply_markup=main_menu())

# ── КНОПКИ ─────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "Каталог")
def catalog(msg):
    if not products:
        bot.send_message(msg.chat.id, "Каталог пуст")
        return

    text = "📦 Товары:\n\n"
    for p in products.values():
        text += f"{p['name']} — {p['price']} руб\n"
    bot.send_message(msg.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "Помощь")
def help_cmd(msg):
    bot.send_message(msg.chat.id, "Напиши что тебе нужно — подберу вариант")

@bot.message_handler(func=lambda m: m.text == "Контакты")
def contacts(msg):
    bot.send_message(msg.chat.id, "Менеджер: @your_username")

# ── ADMIN ──────────────────────────────────────────
@bot.message_handler(func=lambda m: m.text == "Добавить товар" and m.chat.id == ADMIN_ID)
def add_product(msg):
    user_state[msg.chat.id] = {"step": "name"}
    bot.send_message(msg.chat.id, "Название товара?")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, {}).get("step") == "name")
def add_name(msg):
    user_state[msg.chat.id]["name"] = msg.text
    user_state[msg.chat.id]["step"] = "price"
    bot.send_message(msg.chat.id, "Цена?")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, {}).get("step") == "price")
def add_price(msg):
    products[msg.text.lower()] = {
        "name": user_state[msg.chat.id]["name"],
        "price": msg.text
    }
    user_state.pop(msg.chat.id)
    save_data()
    bot.send_message(msg.chat.id, "✅ Добавлено")

@bot.message_handler(func=lambda m: m.text == "Лиды" and m.chat.id == ADMIN_ID)
def leads(msg):
    if not crm:
        bot.send_message(msg.chat.id, "Лидов нет")
        return

    text = "📊 Лиды:\n\n"
    for uid, d in crm.items():
        text += f"{d['name']} (@{d['username']})\n{d['status']}\n\n"
    bot.send_message(msg.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "Рассылка" and m.chat.id == ADMIN_ID)
def broadcast(msg):
    user_state[msg.chat.id] = {"step": "broadcast"}
    bot.send_message(msg.chat.id, "Текст рассылки?")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, {}).get("step") == "broadcast")
def do_broadcast(msg):
    for uid in crm:
        try:
            bot.send_message(uid, msg.text)
        except:
            pass
    user_state.pop(msg.chat.id)
    bot.send_message(msg.chat.id, "✅ Отправлено")

# ── AI ЛОГИКА ──────────────────────────────────────
@bot.message_handler(func=lambda m: True)
def ai(msg):
    if msg.chat.id == ADMIN_ID:
        return

    text = msg.text.lower()

    if any(x in text for x in ["купить", "заказать", "беру"]):
        save_lead(msg, "hot")
        notify_admin(msg)
        bot.send_message(msg.chat.id, "🔥 Отлично! Менеджер скоро напишет")
        return

    save_lead(msg, "warm")
    bot.send_message(msg.chat.id, "Понял, подбираю вариант...")

# ── WEBHOOK ────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok"

@app.route("/")
def home():
    return "OK"

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=BASE_URL + "/webhook")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
