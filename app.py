import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading, sys

# ============================================================
# CONFIG
# ============================================================
TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))
YANDEX_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER = os.environ.get("YANDEX_FOLDER_ID")
BASE_URL = os.environ.get("BASE_URL")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ============================================================
# DATA
# ============================================================
FILE = "db.json"
lock = threading.Lock()

def load():
    if os.path.exists(FILE):
        return json.load(open(FILE))
    return {"products": {}, "crm": {}}

def save():
    with lock:
        json.dump({"products": products, "crm": crm}, open(FILE, "w"), indent=2)

db = load()
products = db["products"]
crm = db["crm"]

# ============================================================
# FILTERS
# ============================================================
BUTTONS = {
    "Каталог","Написать менеджеру","Помощь","Контакты",
    "Главное меню","Добавить товар","Удалить товар",
    "Все товары","Лиды","Рассылка","Отмена"
}

# ============================================================
# AI
# ============================================================
def ask_ai(chat_id, text):
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    catalog = ""
    for p in products.values():
        catalog += f"- {p['name']} ({p['price']} руб.)\n"

    system = f"""
Ты сильный продавец.

Каталог:
{catalog}

Отвечай коротко и по делу.

В конце добавь:
[STATUS:HOT/WARM/COLD]
"""

    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER}/yandexgpt-lite",
        "completionOptions": {"temperature": 0.7},
        "messages": [
            {"role": "system", "text": system},
            {"role": "user", "text": text}
        ]
    }

    headers = {
        "Authorization": f"Api-Key {YANDEX_KEY}",
        "Content-Type": "application/json"
    }

    r = requests.post(url, headers=headers, json=payload)
    raw = r.json()["result"]["alternatives"][0]["message"]["text"]

    status = "cold"
    if "HOT" in raw: status = "hot"
    elif "WARM" in raw: status = "warm"

    clean = re.sub(r"\[.*?\]", "", raw).strip()
    return clean, status

# ============================================================
# CRM
# ============================================================
def save_lead(msg, status, reply):
    uid = str(msg.chat.id)

    if uid not in crm:
        crm[uid] = {
            "name": msg.from_user.first_name,
            "status": status,
            "history": []
        }

    crm[uid]["status"] = status
    crm[uid]["history"].append({"user": msg.text, "bot": reply})

    save()

def notify_hot(msg):
    if msg.chat.id == ADMIN_ID:
        return
    bot.send_message(ADMIN_ID, f"🔥 HOT ЛИД:\n{msg.text}")

# ============================================================
# UI
# ============================================================
def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Каталог","Написать менеджеру","Помощь","Контакты")
    return kb

def admin_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Добавить товар","Удалить товар","Все товары","Лиды")
    return kb

# ============================================================
# START
# ============================================================
@bot.message_handler(commands=["start"])
def start(msg):
    if msg.chat.id == ADMIN_ID:
        bot.send_message(msg.chat.id,"Админ панель",reply_markup=admin_kb())
    else:
        bot.send_message(msg.chat.id,"Напиши что тебя интересует 👇",reply_markup=main_kb())

# ============================================================
# ADMIN
# ============================================================
@bot.message_handler(func=lambda m: m.chat.id == ADMIN_ID and m.text == "Лиды")
def leads(msg):
    text = "Лиды:\n\n"
    for uid, d in crm.items():
        text += f"{d['name']} — {d['status']}\n"
    bot.send_message(msg.chat.id,text)

# ============================================================
# CLIENT BUTTONS
# ============================================================
@bot.message_handler(func=lambda m: m.text == "Каталог")
def catalog(msg):
    if not products:
        bot.send_message(msg.chat.id,"Каталог пуст")
        return
    text = ""
    for p in products.values():
        text += f"{p['name']} — {p['price']} руб\n"
    bot.send_message(msg.chat.id,text)

@bot.message_handler(func=lambda m: m.text == "Помощь")
def help_cmd(msg):
    bot.send_message(msg.chat.id,"Напиши что ищешь и бюджет")

# ============================================================
# AI ONLY (ЧИСТЫЙ)
# ============================================================
@bot.message_handler(func=lambda m: m.text and m.text not in BUTTONS)
def ai(msg):
    # ❗ админ не идёт в AI
    if msg.chat.id == ADMIN_ID:
        return

    try:
        answer, status = ask_ai(msg.chat.id, msg.text)

        save_lead(msg, status, answer)

        bot.send_message(msg.chat.id, answer)

        if status == "hot":
            notify_hot(msg)

    except:
        bot.send_message(msg.chat.id,"Ошибка, попробуй ещё раз")

# ============================================================
# WEBHOOK
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok"

@app.route("/")
def home():
    return "OK"

# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=BASE_URL + "/webhook")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
