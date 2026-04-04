import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading

# ================= ENV =================
ADMIN_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
YANDEX_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FID = os.getenv("YANDEX_FOLDER_ID")
BASE_URL = os.getenv("BASE_URL")
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "")

app = Flask(__name__)

bots = {}
owners = {}

BOTS_FILE = "bots.json"

# ================= FILE STORAGE =================
def load_bots():
    if os.path.exists(BOTS_FILE):
        with open(BOTS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_bots(data):
    with open(BOTS_FILE, "w") as f:
        json.dump(data, f)

# ================= GOOGLE =================
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

# ================= CREATE BOT =================
def create_bot(token, owner_id):

    if token in bots:
        return

    bot = telebot.TeleBot(token)
    print("INIT BOT:", token)

    DATA_FILE = f"db_{token[:8]}.json"
    lock = threading.Lock()

    def load_data():
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"products": {}, "crm": {}}

    def save_data():
        with lock:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"products": products, "crm": crm}, f, ensure_ascii=False)

    raw = load_data()
    products = raw.get("products", {})
    crm = raw.get("crm", {})

    user_state = {}
    follow_flags = {}

    def is_admin(msg):
        return msg.chat.id == owner_id

    # ================= AI =================
    def ask_ai(chat_id, text):
        try:
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            r = requests.post(url,
                headers={"Authorization": f"Api-Key {YANDEX_KEY}"},
                json={
                    "modelUri": f"gpt://{YANDEX_FID}/yandexgpt-lite",
                    "completionOptions": {"temperature": 0.6, "maxTokens": 200},
                    "messages": [{"role": "user", "text": text}]
                }, timeout=10
            )
            answer = r.json()["result"]["alternatives"][0]["message"]["text"]

            status = "warm"
            if any(w in text.lower() for w in ["купить","заказать","беру","оформить"]):
                status = "hot"

            return answer, status
        except Exception as e:
            print("AI ERROR:", e)
            return "Напиши подробнее 👇", "cold"

    # ================= CRM =================
    def save_lead(msg, status):
        uid = str(msg.chat.id)
        name = msg.from_user.first_name or ""
        username = msg.from_user.username or ""

        crm[uid] = {
            "name": name,
            "username": username,
            "status": status,
            "last": msg.text
        }

        save_data()
        save_to_sheet(uid, name, username, status, msg.text)

        if status == "hot":
            try:
                bot.send_message(owner_id, f"🔥 ЛИД\n{name}\n@{username}\n{msg.text}")
            except:
                pass

    # ================= FOLLOW-UP =================
    def follow(chat_id):
        if follow_flags.get(chat_id):
            return
        follow_flags[chat_id] = True

        def worker():
            time.sleep(120)
            if follow_flags.get(chat_id):
                bot.send_message(chat_id, "Остались вопросы?")
            time.sleep(180)
            if follow_flags.get(chat_id):
                bot.send_message(chat_id, "Есть выгодное предложение 🔥")

        threading.Thread(target=worker, daemon=True).start()

    def cancel_follow(chat_id):
        follow_flags[chat_id] = False

    # ================= UI =================
    def main_menu(msg):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("📦 Каталог", "💬 Написать менеджеру")
        if is_admin(msg):
            kb.add("📊 Лиды", "📢 Рассылка")
        return kb

    # ================= HANDLERS =================
    @bot.message_handler(commands=["start"])
    def start(msg):
        bot.send_message(msg.chat.id, "Привет 👋", reply_markup=main_menu(msg))

    @bot.message_handler(func=lambda m: m.text == "📦 Каталог")
    def catalog(msg):
        if not products:
            bot.send_message(msg.chat.id, "Каталог пуст")
            return
        text = "\n".join([f"{p['name']} — {p['price']}" for p in products.values()])
        bot.send_message(msg.chat.id, text)

    @bot.message_handler(func=lambda m: m.text == "📊 Лиды")
    def leads(msg):
        if not is_admin(msg):
            return
        for uid, d in crm.items():
            bot.send_message(msg.chat.id, f"{d['name']} — {d['status']}")

    @bot.message_handler(func=lambda m: m.text == "📢 Рассылка")
    def broadcast_start(msg):
        if not is_admin(msg):
            return
        user_state[msg.chat.id] = "broadcast"
        bot.send_message(msg.chat.id, "Введите текст")

    @bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "broadcast")
    def broadcast_send(msg):
        for uid in crm:
            try:
                bot.send_message(int(uid), msg.text)
            except:
                pass
        user_state[msg.chat.id] = None
        bot.send_message(msg.chat.id, "Готово")

    # ================= AI =================
    @bot.message_handler(func=lambda m: True)
    def all(msg):

        cancel_follow(msg.chat.id)

        text = msg.text.lower()

        if "купить" in text:
            bot.send_message(msg.chat.id, "🔥 Оформляем!")
            save_lead(msg, "hot")
            return

        answer, status = ask_ai(msg.chat.id, msg.text)
        bot.send_message(msg.chat.id, answer)
        save_lead(msg, status)

        follow(msg.chat.id)

    bots[token] = bot
    owners[token] = owner_id

    # сохраняем бота
    data = load_bots()
    data[token] = owner_id
    save_bots(data)

    bot.remove_webhook()
    bot.set_webhook(url=f"{BASE_URL}/bot/{token}")

# ================= ADMIN BOT =================
admin_bot = telebot.TeleBot(ADMIN_TOKEN)

@admin_bot.message_handler(commands=["start"])
def admin_start(msg):
    admin_bot.send_message(msg.chat.id, "🚀 Пришли токен бота")

@admin_bot.message_handler(func=lambda m: True)
def connect(msg):
    token = msg.text.strip()
    try:
        test = telebot.TeleBot(token)
        me = test.get_me()

        create_bot(token, msg.chat.id)

        admin_bot.send_message(msg.chat.id, f"✅ @{me.username} подключен")
    except Exception as e:
        print("CONNECT ERROR:", e)
        admin_bot.send_message(msg.chat.id, "❌ Ошибка токена")

# ================= WEBHOOK =================
@app.route("/bot/<token>", methods=["POST"])
def webhook(token):
    print("WEBHOOK HIT:", token)

    if token not in bots:
        print("BOT NOT FOUND, RELOADING...")
        saved = load_bots()
        if token in saved:
            create_bot(token, saved[token])
        else:
            return "no bot"

    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bots[token].process_new_updates([update])

    return "ok"

@app.route("/")
def home():
    return "SaaS GOD MODE ON"

# ================= START =================
if __name__ == "__main__":

    # загружаем всех ботов при старте
    saved = load_bots()
    for token, owner_id in saved.items():
        print("RELOAD BOT:", token)
        create_bot(token, owner_id)

    # админ бот
    create_bot(ADMIN_TOKEN, ADMIN_ID)

    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
