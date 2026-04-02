import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading

# ================= CONFIG =================
TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
SUPER_ADMIN = int(os.environ.get("ADMIN_ID"))
YANDEX_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER = os.environ.get("YANDEX_FOLDER_ID")
BASE_URL = os.environ.get("BASE_URL")

bot = telebot.TeleBot(TOKEN)
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

Отвечай кратко, уверенно и продавай.

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
            (300, "Могу предложить лучший вариант 🔥"),
            (600, "Есть выгодное предложение")
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
        bot.send_message(msg.chat.id, "Напиши, что ищешь 👇", reply_markup=client_kb())

# ================= CREATE CLIENT =================
@bot.message_handler(commands=["create_client"])
def create(msg):
    if msg.chat.id != SUPER_ADMIN:
        return
    cid = create_client(msg.chat.id)
    bot.send_message(msg.chat.id, f"Клиент создан: {cid}")

# ================= SUB =================
@bot.message_handler(commands=["activate"])
def activate(msg):
    cid, client = get_client(msg.chat.id)

    if not client:
        cid = create_client(msg.chat.id)
        client = clients[cid]

    client["subscription_until"] = time.time() + 30*86400
    save()

    bot.send_message(msg.chat.id, "Подписка активна")

# ================= PRODUCTS =================
@bot.message_handler(func=lambda m: m.text == "Добавить товар")
def add(msg):
    cid, client = get_client(msg.chat.id)
    if not client or msg.chat.id != client["owner_id"]:
        return

    user_state[msg.chat.id] = {"step": "name"}
    bot.send_message(msg.chat.id, "Название?")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step")=="name")
def add_name(msg):
    user_state[msg.chat.id]["name"] = msg.text
    user_state[msg.chat.id]["step"] = "price"
    bot.send_message(msg.chat.id, "Цена?")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step")=="price")
def add_price(msg):
    user_state[msg.chat.id]["price"] = float(msg.text)
    user_state[msg.chat.id]["step"] = "stock"
    bot.send_message(msg.chat.id, "Количество?")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step")=="stock")
def add_stock(msg):
    cid, client = get_client(msg.chat.id)
    d = user_state[msg.chat.id]

    client["products"][d["name"].lower()] = {
        "name": d["name"],
        "price": d["price"],
        "stock": int(msg.text)
    }

    user_state.pop(msg.chat.id)
    save()

    bot.send_message(msg.chat.id, "Товар добавлен", reply_markup=owner_kb())

# ================= DELETE =================
@bot.message_handler(func=lambda m: m.text == "Удалить товар")
def delete(msg):
    cid, client = get_client(msg.chat.id)
    if not client or msg.chat.id != client["owner_id"]:
        return

    text = "\n".join([p["name"] for p in client["products"].values()])
    user_state[msg.chat.id] = {"step": "del"}
    bot.send_message(msg.chat.id, f"Товары:\n{text}\n\nЧто удалить?")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step")=="del")
def delete_do(msg):
    cid, client = get_client(msg.chat.id)
    key = msg.text.lower()

    if key in client["products"]:
        client["products"].pop(key)
        save()
        bot.send_message(msg.chat.id, "Удалено", reply_markup=owner_kb())

# ================= LEADS =================
@bot.message_handler(func=lambda m: m.text == "Лиды")
def leads(msg):
    cid, client = get_client(msg.chat.id)
    if not client or msg.chat.id != client["owner_id"]:
        return

    text = ""
    for uid, d in client["crm"].items():
        text += f"{uid} — {d['status']}\n"

    bot.send_message(msg.chat.id, text or "Нет лидов")

# ================= BROADCAST =================
@bot.message_handler(func=lambda m: m.text == "Рассылка")
def bc(msg):
    user_state[msg.chat.id] = {"step": "bc"}
    bot.send_message(msg.chat.id, "Текст рассылки?")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id,{}).get("step")=="bc")
def bc_send(msg):
    cid, client = get_client(msg.chat.id)

    for uid in client["crm"]:
        try:
            bot.send_message(int(uid), msg.text)
        except:
            pass

    user_state.pop(msg.chat.id)
    bot.send_message(msg.chat.id, "Готово", reply_markup=owner_kb())

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
