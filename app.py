import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading

# ================= SAFE ENV =================
def get_env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"❌ ENV MISSING: {name}")
    else:
        print(f"✅ ENV {name}: OK")
    return val

BOT_TOKEN = get_env("ADMIN_BOT_TOKEN", required=True)
ADMIN_ID_RAW = get_env("ADMIN_ID", required=True)
YANDEX_KEY = get_env("YANDEX_API_KEY", required=True)
YANDEX_FOLDER = get_env("YANDEX_FOLDER_ID", required=True)
BASE_URL = get_env("BASE_URL", required=True)

try:
    SUPER_ADMIN = int(ADMIN_ID_RAW)
except:
    print("❌ ADMIN_ID НЕ ЧИСЛО")
    SUPER_ADMIN = 0

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ================= DATABASE =================
FILE = "saas_db.json"
lock = threading.Lock()

def load():
    if os.path.exists(FILE):
        return json.load(open(FILE))
    return {"clients": {}}

def save():
    with lock:
        json.dump(db, open(FILE, "w"), indent=2)

db = load()
clients = db["clients"]

# ================= GLOBAL =================
user_state = {}
follow_events = {}
last_notify = {}

BUTTONS = {
    "Каталог","Помощь","Товары","Лиды",
    "Добавить товар","Удалить товар",
    "Рассылка","Главное меню","Отмена"
}

# ================= CLIENT SYSTEM =================
def create_client(owner_id):
    cid = str(owner_id)
    clients[cid] = {
        "owner_id": owner_id,
        "products": {},
        "crm": {},
        "subscription_until": time.time() + 7*86400
    }
    save()
    return cid

def get_client(user_id):
    for cid, c in clients.items():
        if user_id == c["owner_id"]:
            return cid, c
        if str(user_id) in c["crm"]:
            return cid, c
    return None, None

def check_sub(client):
    return time.time() < client["subscription_until"]

# ================= MEMORY =================
def build_memory(client, uid):
    history = client["crm"].get(uid, {}).get("history", [])[-6:]
    return "\n".join([f"Клиент: {h['user']} Бот: {h['bot']}" for h in history])

# ================= AI =================
def ask_ai(client, uid, text):
    memory = build_memory(client, uid)
    catalog = "\n".join([f"- {p['name']} ({p['price']})" for p in client["products"].values()])

    system = f"""
Ты топовый продавец.

ПАМЯТЬ:
{memory}

КАТАЛОГ:
{catalog}

Отвечай кратко и продавай.

[STATUS:HOT/WARM/COLD]
"""

    r = requests.post(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
        headers={"Authorization": f"Api-Key {YANDEX_KEY}"},
        json={
            "modelUri": f"gpt://{YANDEX_FOLDER}/yandexgpt-lite",
            "messages": [
                {"role": "system", "text": system},
                {"role": "user", "text": text}
            ]
        },
        timeout=20
    )

    raw = r.json()["result"]["alternatives"][0]["message"]["text"]

    status = "cold"
    if "HOT" in raw: status = "hot"
    elif "WARM" in raw: status = "warm"

    clean = re.sub(r"\[.*?\]", "", raw).strip()
    return clean, status

# ================= CRM =================
def save_lead(client, msg, status, reply):
    uid = str(msg.chat.id)

    if uid not in client["crm"]:
        client["crm"][uid] = {
            "status": status,
            "history": [],
            "last_time": time.time()
        }

    client["crm"][uid]["status"] = status
    client["crm"][uid]["history"].append({
        "user": msg.text[:500],
        "bot": reply[:500]
    })
    client["crm"][uid]["last_time"] = time.time()

    save()

# ================= NOTIFY =================
def notify_owner(client, msg, status):
    owner = client["owner_id"]

    if msg.chat.id == owner:
        return

    uid = str(msg.chat.id)
    now = time.time()

    if uid in last_notify and now - last_notify[uid] < 300:
        return

    last_notify[uid] = now

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "💬 Ответить",
        url=f"tg://user?id={msg.chat.id}"
    ))

    bot.send_message(
        owner,
        f"{status.upper()} ЛИД\n{msg.text}",
        reply_markup=kb
    )

# ================= FOLLOW UP =================
def start_follow(client, uid):
    if uid in follow_events:
        follow_events[uid].set()

    ev = threading.Event()
    follow_events[uid] = ev

    def worker():
        steps = [
            (120, "Есть вопросы? Помогу 👍"),
            (300, "Есть лучший вариант 🔥"),
            (600, "Последнее предложение")
        ]
        for delay, text in steps:
            if ev.wait(delay):
                return
            try:
                bot.send_message(int(uid), text)
            except:
                return

    threading.Thread(target=worker, daemon=True).start()

# ================= UI =================
def owner_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Товары","Лиды","Добавить товар","Удалить товар","Рассылка")
    return kb

def client_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Каталог","Помощь")
    return kb

# ================= START =================
@bot.message_handler(commands=["start"])
def start(msg):
    user_state.pop(msg.chat.id, None)

    if msg.chat.id == SUPER_ADMIN:
        bot.send_message(msg.chat.id, "Ты супер-админ\n/create_client")
        return

    cid, client = get_client(msg.chat.id)

    if not client:
        bot.send_message(msg.chat.id, "Нет доступа")
        return

    if msg.chat.id == client["owner_id"]:
        bot.send_message(msg.chat.id, "Панель", reply_markup=owner_kb())
    else:
        bot.send_message(msg.chat.id, "Напиши что ищешь 👇", reply_markup=client_kb())

# ================= AI =================
@bot.message_handler(func=lambda m: m.text and m.text not in BUTTONS)
def ai(msg):
    cid, client = get_client(msg.chat.id)

    if not client:
        return

    if not check_sub(client):
        bot.send_message(msg.chat.id, "Подписка истекла")
        return

    if msg.chat.id == client["owner_id"]:
        return

    ans, status = ask_ai(client, str(msg.chat.id), msg.text)

    save_lead(client, msg, status, ans)
    bot.send_message(msg.chat.id, ans)

    if status in ["hot","warm"]:
        notify_owner(client, msg, status)

    start_follow(client, str(msg.chat.id))

# ================= WEBHOOK =================
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
