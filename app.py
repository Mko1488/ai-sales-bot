import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading, sys

# ── ЧИТАЕМ ПЕРЕМЕННЫЕ ──────────────────────────────────────────
BOT_TOKEN  = os.environ.get("ADMIN_BOT_TOKEN", "")
ADMIN_ID   = os.environ.get("ADMIN_ID", "")
YANDEX_KEY = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FID = os.environ.get("YANDEX_FOLDER_ID", "")
BASE_URL   = os.environ.get("BASE_URL", "")

# ── ПРОВЕРКА ───────────────────────────────────────────────────
missing = []
if not BOT_TOKEN:  missing.append("ADMIN_BOT_TOKEN")
if not ADMIN_ID:   missing.append("ADMIN_ID")
if not YANDEX_KEY: missing.append("YANDEX_API_KEY")
if not YANDEX_FID: missing.append("YANDEX_FOLDER_ID")
if not BASE_URL:   missing.append("BASE_URL")

if missing:
    print("НЕТ ПЕРЕМЕННЫХ: " + ", ".join(missing))
    sys.exit(1)

ADMIN_ID = int(ADMIN_ID)

# ── ИНИЦИАЛИЗАЦИЯ ──────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ── БАЗА ДАННЫХ ────────────────────────────────────────────────
DATA_FILE = "db.json"
_lock = threading.Lock()

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"products": {}, "crm": {}}

def save_data():
    with _lock:
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"products": products, "crm": crm}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("Ошибка сохранения:", e)

_raw     = load_data()
products = _raw.get("products", {})
crm      = _raw.get("crm", {})

# ── СОСТОЯНИЕ ──────────────────────────────────────────────────
user_state       = {}
follow_up_events = {}
ADMIN_STEPS      = {"add_name", "add_price", "add_stock", "delete_name", "broadcast_text"}

def get_step(chat_id):
    return user_state.get(chat_id, {}).get("step")

def clear_state(chat_id):
    user_state.pop(chat_id, None)

def is_admin(msg):
    return msg.chat.id == ADMIN_ID

# ── FOLLOW-UP ──────────────────────────────────────────────────
def cancel_follow_up(chat_id):
    ev = follow_up_events.get(chat_id)
    if ev:
        ev.set()

def start_follow_up(chat_id):
    cancel_follow_up(chat_id)
    ev = threading.Event()
    follow_up_events[chat_id] = ev
    threading.Thread(target=_follow_up_worker, args=(chat_id, ev), daemon=True).start()

def _follow_up_worker(chat_id, stop_ev):
    msgs = [
        (3 * 60,  "Остались вопросы? Готов помочь с выбором"),
        (7 * 60,  "Есть варианты под ваш запрос. Показать?"),
        (15 * 60, "Последнее сообщение — есть выгодное предложение. Напишите да!"),
    ]
    for delay, text in msgs:
        if stop_ev.wait(timeout=delay):
            return
        try:
            bot.send_message(chat_id, text)
        except Exception:
            return

# ── YANDEX GPT ─────────────────────────────────────────────────
def ask_ai(chat_id, user_text):
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    catalog = ""
    if products:
        catalog = "\nКАТАЛОГ:\n"
        for p in products.values():
            avail = f"в наличии {p['stock']} шт." if p["stock"] > 0 else "нет в наличии"
            catalog += f"- {p['name']}: {p['price']} руб. ({avail})\n"

    system = (
        "Ты — менеджер по продажам. Коротко (2-4 предложения), по-человечески.\n"
        "Не выдумывай товары. Если готов купить — предложи написать «оформить».\n"
        "В конце добавь одну строку:\n"
        "[STATUS:HOT] — готов купить\n"
        "[STATUS:WARM] — интересуется\n"
        "[STATUS:COLD] — просто смотрит\n" + catalog
    )

    uid = str(chat_id)
    history = []
    if uid in crm:
        for h in crm[uid].get("history", [])[-6:]:
            history.append({"role": "user" if h["role"] == "user" else "assistant", "text": h["text"]})

    messages = [{"role": "system", "text": system}] + history + [{"role": "user", "text": user_text}]

    resp = requests.post(
        url,
        headers={"Authorization": f"Api-Key {YANDEX_KEY}", "Content-Type": "application/json"},
        json={
            "modelUri": f"gpt://{YANDEX_FID}/yandexgpt-lite",
            "completionOptions": {"temperature": 0.65, "maxTokens": 350},
            "messages": messages
        },
        timeout=20
    )
    resp.raise_for_status()
    raw = resp.json()["result"]["alternatives"][0]["message"]["text"]

    status = "cold"
    m = re.search(r"\[STATUS:(HOT|WARM|COLD)\]", raw, re.IGNORECASE)
    if m:
        status = {"HOT": "hot", "WARM": "warm", "COLD": "cold"}.get(m.group(1).upper(), "cold")

    clean = re.sub(r"\[STATUS:.*?\]", "", raw, flags=re.IGNORECASE).strip()
    return clean, status

# ── CRM ────────────────────────────────────────────────────────
RANK  = {"hot": 2, "warm": 1, "cold": 0}
LABEL = {"hot": "Горячий", "warm": "Теплый", "cold": "Холодный"}

def save_lead(msg, status, ai_reply=None):
    uid   = str(msg.chat.id)
    name  = (msg.from_user.first_name or "Без имени")[:30]
    uname = msg.from_user.username or ""

    if uid not in crm:
        crm[uid] = {"name": name, "username": uname, "status": status, "history": [], "created_at": int(time.time())}

    if RANK.get(status, 0) > RANK.get(crm[uid].get("status", "cold"), 0):
        crm[uid]["status"] = status

    crm[uid]["history"].append({"role": "user", "text": msg.text[:500], "ts": int(time.time())})
    if ai_reply:
        crm[uid]["history"].append({"role": "assistant", "text": ai_reply[:500], "ts": int(time.time())})
    save_data()

def notify_hot(msg):
    try:
        name  = msg.from_user.first_name or "Без имени"
        uname = f"@{msg.from_user.username}" if msg.from_user.username else "нет username"
        bot.send_message(ADMIN_ID,
            f"ГОРЯЧИЙ ЛИД!\n\nИмя: {name} ({uname})\nID: {msg.chat.id}\n"
            f"Сообщение: {msg.text}\n\nНаписать: tg://user?id={msg.chat.id}")
    except Exception as e:
        print("Ошибка уведомления:", e)

# ── КЛАВИАТУРЫ ─────────────────────────────────────────────────
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("Каталог", "Написать менеджеру", "Помощь", "Контакты")
    return kb

def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("Добавить товар", "Удалить товар", "Все товары", "Лиды", "Рассылка", "Главное меню")
    return kb

def cancel_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("Отмена")
    return kb

# ── ХЕНДЛЕРЫ ───────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(msg):
    clear_state(msg.chat.id)
    name = msg.from_user.first_name or "друг"
    bot.send_message(msg.chat.id, f"Привет, {name}!\nПомогу подобрать лучшее предложение.", reply_markup=main_menu())

@bot.message_handler(commands=["admin"])
def cmd_admin(msg):
    if not is_admin(msg):
        bot.send_message(msg.chat.id, "Нет доступа")
        return
    clear_state(msg.chat.id)
    bot.send_message(msg.chat.id, "Панель управления:", reply_markup=admin_menu())

@bot.message_handler(commands=["leads"])
def cmd_leads(msg):
    if not is_admin(msg): return
    _show_leads(msg)

@bot.message_handler(func=lambda m: m.text == "Отмена")
def btn_cancel(msg):
    clear_state(msg.chat.id)
    kb = admin_menu() if is_admin(msg) else main_menu()
    bot.send_message(msg.chat.id, "Отменено.", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "Каталог")
def btn_catalog(msg):
    clear_state(msg.chat.id)
    if not products:
        bot.send_message(msg.chat.id, "Каталог пока пустой. Напишите — подберём!")
        return
    text = "Наш каталог:\n\n"
    for p in products.values():
        avail = f"В наличии: {p['stock']} шт." if p["stock"] > 0 else "Нет в наличии"
        text += f"{p['name']}\n{p['price']} руб.\n{avail}\n\n"
    bot.send_message(msg.chat.id, text + "Напишите название — расскажу подробнее")

@bot.message_handler(func=lambda m: m.text == "Написать менеджеру")
def btn_write(msg):
    clear_state(msg.chat.id)
    bot.send_message(msg.chat.id, "Напишите ваш вопрос — отвечу прямо сейчас")

@bot.message_handler(func=lambda m: m.text == "Помощь")
def btn_help(msg):
    clear_state(msg.chat.id)
    bot.send_message(msg.chat.id, "Напишите:\n- Что интересует\n- Бюджет\n- Для каких задач\n\nПодберу лучший вариант!")

@bot.message_handler(func=lambda m: m.text == "Контакты")
def btn_contacts(msg):
    clear_state(msg.chat.id)
    bot.send_message(msg.chat.id, "Менеджер: @ВАШ_ЮЗЕРНЕЙМ\nВремя: 9:00-21:00\n\nИли напишите сюда!")

@bot.message_handler(func=lambda m: m.text == "Главное меню")
def btn_home(msg):
    clear_state(msg.chat.id)
    bot.send_message(msg.chat.id, "Главное меню:", reply_markup=main_menu())

# Добавить товар
@bot.message_handler(func=lambda m: m.text == "Добавить товар")
def btn_add(msg):
    if not is_admin(msg): return
    user_state[msg.chat.id] = {"step": "add_name"}
    bot.send_message(msg.chat.id, "Введите название товара:", reply_markup=cancel_kb())

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_name")
def step_add_name(msg):
    if msg.text == "Отмена": return
    user_state[msg.chat.id]["name"] = msg.text.strip()
    user_state[msg.chat.id]["step"] = "add_price"
    bot.send_message(msg.chat.id, "Введите цену (например 1500):", reply_markup=cancel_kb())

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_price")
def step_add_price(msg):
    if msg.text == "Отмена": return
    try:
        user_state[msg.chat.id]["price"] = float(msg.text.replace(",", ".").strip())
        user_state[msg.chat.id]["step"] = "add_stock"
        bot.send_message(msg.chat.id, "Введите количество в наличии:", reply_markup=cancel_kb())
    except ValueError:
        bot.send_message(msg.chat.id, "Нужно число. Например: 1500")

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_stock")
def step_add_stock(msg):
    if msg.text == "Отмена": return
    try:
        d = user_state[msg.chat.id]
        products[d["name"].lower()] = {"name": d["name"], "price": d["price"], "stock": int(msg.text.strip())}
        clear_state(msg.chat.id)
        save_data()
        bot.send_message(msg.chat.id, f"Товар «{d['name']}» добавлен!", reply_markup=admin_menu())
    except ValueError:
        bot.send_message(msg.chat.id, "Нужно целое число. Например: 10")

# Удалить товар
@bot.message_handler(func=lambda m: m.text == "Удалить товар")
def btn_delete(msg):
    if not is_admin(msg): return
    if not products:
        bot.send_message(msg.chat.id, "Товаров нет")
        return
    names = "\n".join(f"- {v['name']}" for v in products.values())
    user_state[msg.chat.id] = {"step": "delete_name"}
    bot.send_message(msg.chat.id, f"Товары:\n{names}\n\nВведите название для удаления:", reply_markup=cancel_kb())

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "delete_name")
def step_delete(msg):
    if msg.text == "Отмена": return
    key = msg.text.strip().lower()
    if key in products:
        name = products.pop(key)["name"]
        clear_state(msg.chat.id)
        save_data()
        bot.send_message(msg.chat.id, f"«{name}» удалён", reply_markup=admin_menu())
    else:
        bot.send_message(msg.chat.id, "Не найден. Проверьте написание или нажмите «Отмена».")

# Все товары
@bot.message_handler(func=lambda m: m.text == "Все товары")
def btn_all_products(msg):
    if not is_admin(msg): return
    if not products:
        bot.send_message(msg.chat.id, "Товаров нет")
        return
    text = "Все товары:\n\n" + "\n".join(f"- {p['name']} — {p['price']} руб. | {p['stock']} шт." for p in products.values())
    bot.send_message(msg.chat.id, text)

# Лиды
@bot.message_handler(func=lambda m: m.text == "Лиды")
def btn_leads(msg):
    if not is_admin(msg): return
    _show_leads(msg)

def _show_leads(msg):
    if not crm:
        bot.send_message(msg.chat.id, "Лидов пока нет")
        return
    sorted_leads = sorted(crm.items(), key=lambda x: RANK.get(x[1].get("status", "cold"), 0), reverse=True)
    text = "CRM — Лиды:\n\n"
    for uid, d in sorted_leads[:20]:
        name   = d.get("name", "?")
        uname  = f"@{d['username']}" if d.get("username") else ""
        status = LABEL.get(d.get("status", "cold"), "?")
        msgs   = [h for h in d.get("history", []) if h["role"] == "user"]
        last   = f"\n   Последнее: {msgs[-1]['text'][:40]}..." if msgs else ""
        text  += f"{status} — {name} {uname}\n   ID: {uid} | сообщений: {len(msgs)}{last}\n\n"
    if len(crm) > 20:
        text += f"...и ещё {len(crm) - 20} лидов"
    for i in range(0, len(text), 4000):
        bot.send_message(msg.chat.id, text[i:i+4000])

# Рассылка
@bot.message_handler(func=lambda m: m.text == "Рассылка")
def btn_broadcast(msg):
    if not is_admin(msg): return
    user_state[msg.chat.id] = {"step": "broadcast_text"}
    bot.send_message(msg.chat.id, f"Введите текст рассылки ({len(crm)} клиентов):", reply_markup=cancel_kb())

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "broadcast_text")
def step_broadcast(msg):
    if msg.text == "Отмена": return
    text = msg.text
    clear_state(msg.chat.id)
    sent = failed = 0
    for uid in crm:
        try:
            bot.send_message(int(uid), text)
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1
    bot.send_message(msg.chat.id, f"Готово! Отправлено: {sent}, ошибок: {failed}", reply_markup=admin_menu())

# AI-продавец
@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def ai_seller(msg):
    if get_step(msg.chat.id) in ADMIN_STEPS:
        return

    cancel_follow_up(msg.chat.id)
    text_lower = msg.text.lower()

    for key, item in products.items():
        if key in text_lower:
            avail = f"В наличии: {item['stock']} шт." if item["stock"] > 0 else "Сейчас нет, но могу записать в список ожидания"
            reply = f"{item['name']} — {item['price']} руб.\n{avail}\n\nОформляем? Напишите «оформить» или задайте вопрос"
            save_lead(msg, "warm", reply)
            bot.send_message(msg.chat.id, reply)
            start_follow_up(msg.chat.id)
            return

    if any(w in text_lower for w in ["оформить", "беру", "заказать", "хочу купить", "оплатить", "покупаю"]):
        reply = "Отлично! Запрос принят.\nМенеджер свяжется с вами в ближайшее время!\nНапишите удобное время для связи"
        save_lead(msg, "hot", reply)
        notify_hot(msg)
        bot.send_message(msg.chat.id, reply)
        return

    try:
        answer, status = ask_ai(msg.chat.id, msg.text)
        save_lead(msg, status, answer)
        bot.send_message(msg.chat.id, answer)
        if status == "hot":
            notify_hot(msg)
    except Exception as e:
        print("AI error:", e)
        fallback = "Понял! Уточните что вас интересует — подберу лучший вариант"
        save_lead(msg, "cold", fallback)
        bot.send_message(msg.chat.id, fallback)

    start_follow_up(msg.chat.id)

# ── WEBHOOK ────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook_handler():
    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print("Webhook error:", e)
    return "ok"

@app.route("/")
def health():
    return "Bot is running OK"

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=BASE_URL + "/webhook")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
