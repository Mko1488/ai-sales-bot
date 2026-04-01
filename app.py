import telebot
from telebot import types
from flask import Flask, request
import os
import requests
import threading
import time
import json

# ============================================================
#  КОНФИГУРАЦИЯ — задаётся через переменные окружения Railway
# ============================================================
ADMIN_BOT_TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
ADMIN_ID      = int(os.environ.get("ADMIN_ID", "0"))
YANDEX_KEY    = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER = os.environ.get("YANDEX_FOLDER_ID")
BASE_URL      = os.environ.get("BASE_URL")          # https://your-app.up.railway.app

# ============================================================
#  ИНИЦИАЛИЗАЦИЯ
# ============================================================
bot = telebot.TeleBot(ADMIN_BOT_TOKEN)
app = Flask(__name__)

# ============================================================
#  ХРАНИЛИЩЕ (JSON-файл — данные сохраняются между перезапусками)
# ============================================================
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"products": {}, "crm": {}}

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"products": products, "crm": crm}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[SAVE ERROR] {e}")

_raw       = load_data()
products   = _raw.get("products", {})   # { "название_lower": {name, price, stock} }
crm        = _raw.get("crm", {})        # { "user_id": {name, username, status, history, created_at} }

user_state       = {}   # { chat_id: {"step": "...", ...} }  — временное состояние диалога
follow_up_active = {}   # { chat_id: bool } — флаг активного дожима

# ============================================================
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def is_admin(message):
    return message.chat.id == ADMIN_ID

def get_step(chat_id):
    return user_state.get(chat_id, {}).get("step")

def clear_state(chat_id):
    user_state.pop(chat_id, None)

# ============================================================
#  AI — ЯНДЕКС GPT
# ============================================================
def ask_ai(chat_id, user_text):
    """Отправляет запрос в Yandex GPT с историей диалога и каталогом."""
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    # Каталог товаров для контекста
    catalog_ctx = ""
    if products:
        catalog_ctx = "\n\nНаш каталог товаров:\n"
        for item in products.values():
            avail = f"в наличии {item['stock']} шт." if item["stock"] > 0 else "нет в наличии"
            catalog_ctx += f"- {item['name']}: {item['price']}$ ({avail})\n"

    system_text = (
        "Ты — профессиональный менеджер по продажам. Твои задачи:\n"
        "1. Выявить потребность клиента\n"
        "2. Предложить подходящий товар из каталога\n"
        "3. Мягко подтолкнуть к покупке\n\n"
        "Правила:\n"
        "- Отвечай коротко: 2–4 предложения\n"
        "- Пиши по-человечески, уверенно, дружелюбно\n"
        "- Используй эмодзи умеренно (🔥 ✅ 👍)\n"
        "- Не выдумывай товары, которых нет в каталоге\n"
        "- Если клиент готов купить — предложи написать «оформить»"
        + catalog_ctx
    )

    # История последних 6 сообщений
    uid = str(chat_id)
    history_msgs = []
    if uid in crm:
        for h in crm[uid].get("history", [])[-6:]:
            role = "user" if h["role"] == "user" else "assistant"
            history_msgs.append({"role": role, "text": h["text"]})

    messages = [{"role": "system", "text": system_text}]
    messages.extend(history_msgs)
    messages.append({"role": "user", "text": user_text})

    headers = {
        "Authorization": f"Api-Key {YANDEX_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER}/yandexgpt-lite",
        "completionOptions": {"temperature": 0.65, "maxTokens": 300},
        "messages": messages
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()["result"]["alternatives"][0]["message"]["text"]

# ============================================================
#  CRM
# ============================================================
def detect_status(text: str) -> str:
    t = text.lower()
    hot_words  = ["купить", "заказать", "оформить", "беру", "хочу купить", "как заказать", "да", "согласен"]
    warm_words = ["цена", "сколько стоит", "стоимость", "есть ли", "почём", "доставка", "характеристики", "расскажи"]
    if any(w in t for w in hot_words):
        return "🔥 горячий"
    if any(w in t for w in warm_words):
        return "🌡 тёплый"
    return "❄️ холодный"

def save_lead(message, status: str, ai_reply: str = None):
    uid  = str(message.chat.id)
    name = message.from_user.first_name or "Без имени"
    uname = message.from_user.username or ""

    if uid not in crm:
        crm[uid] = {
            "name": name,
            "username": uname,
            "status": status,
            "history": [],
            "created_at": int(time.time())
        }

    # Статус повышается, но не понижается (горячий не может стать холодным)
    rank = {"🔥 горячий": 2, "🌡 тёплый": 1, "❄️ холодный": 0}
    if rank.get(status, 0) > rank.get(crm[uid].get("status", ""), 0):
        crm[uid]["status"] = status

    crm[uid]["history"].append({"role": "user", "text": message.text, "ts": int(time.time())})
    if ai_reply:
        crm[uid]["history"].append({"role": "assistant", "text": ai_reply, "ts": int(time.time())})

    save_data()

def notify_admin_hot(message):
    """Уведомляет администратора о горячем лиде."""
    try:
        name  = message.from_user.first_name or "Без имени"
        uname = f"@{message.from_user.username}" if message.from_user.username else "нет username"
        uid   = message.chat.id
        bot.send_message(
            ADMIN_ID,
            f"🔥 *ГОРЯЧИЙ ЛИД!*\n\n"
            f"👤 {name} ({uname})\n"
            f"🆔 `{uid}`\n"
            f"💬 _{message.text}_\n\n"
            f"➡️ Написать: tg://user?id={uid}",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")

# ============================================================
#  FOLLOW-UP (автодожим)
# ============================================================
def cancel_follow_up(chat_id):
    follow_up_active[chat_id] = False

def follow_up_worker(chat_id):
    follow_up_active[chat_id] = True
    schedule = [
        (120, "💬 Ещё здесь? Готов ответить на любые вопросы 👍"),
        (180, "🔥 Кстати, есть несколько вариантов под ваш запрос. Показать?"),
        (300, "✅ Последнее сообщение на сегодня — есть выгодное предложение. Напишите «да» и расскажу подробнее!"),
    ]
    for delay, text in schedule:
        time.sleep(delay)
        if not follow_up_active.get(chat_id, False):
            return  # Клиент написал — дожим отменён
        try:
            bot.send_message(chat_id, text)
        except Exception:
            return
    follow_up_active[chat_id] = False

def start_follow_up(chat_id):
    t = threading.Thread(target=follow_up_worker, args=(chat_id,), daemon=True)
    t.start()

# ============================================================
#  КЛАВИАТУРЫ
# ============================================================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📦 Каталог", "💬 Написать менеджеру")
    kb.add("❓ Помощь", "📞 Контакты")
    return kb

def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("➕ Добавить товар", "❌ Удалить товар")
    kb.add("📊 Все товары", "📈 Лиды")
    kb.add("🏠 Главное меню")
    return kb

# ============================================================
#  /start
# ============================================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    name = message.from_user.first_name or "друг"
    bot.send_message(
        message.chat.id,
        f"👋 Привет, {name}!\n\n"
        "Я помогу подобрать лучшее предложение под ваши задачи.\n\n"
        "Выберите действие 👇",
        reply_markup=main_menu()
    )

# ============================================================
#  /admin
# ============================================================
@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message):
        bot.send_message(message.chat.id, "⛔ Нет доступа")
        return
    bot.send_message(message.chat.id, "⚙️ *Панель управления*", parse_mode="Markdown", reply_markup=admin_menu())

# ============================================================
#  /leads
# ============================================================
@bot.message_handler(commands=["leads"])
def cmd_leads(message):
    if not is_admin(message):
        return
    _show_leads(message)

# ============================================================
#  КАТАЛОГ (клиент)
# ============================================================
@bot.message_handler(func=lambda m: m.text == "📦 Каталог")
def btn_catalog(message):
    if not products:
        bot.send_message(message.chat.id, "😔 Каталог пока пустой. Напишите — подберём под ваш запрос!")
        return
    text = "📦 *Наш каталог:*\n\n"
    for item in products.values():
        avail = f"✅ В наличии: {item['stock']} шт." if item["stock"] > 0 else "❌ Нет в наличии"
        text += f"*{item['name']}*\n💰 {item['price']}$\n{avail}\n\n"
    text += "Напишите название товара — расскажу подробнее 👇"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# ============================================================
#  ПОМОЩЬ
# ============================================================
@bot.message_handler(func=lambda m: m.text == "❓ Помощь")
def btn_help(message):
    bot.send_message(
        message.chat.id,
        "🤝 Просто напишите:\n\n"
        "• Что вас интересует\n"
        "• Какой у вас бюджет\n"
        "• Для каких задач нужен товар\n\n"
        "И я подберу лучший вариант 👇"
    )

# ============================================================
#  КОНТАКТЫ
# ============================================================
@bot.message_handler(func=lambda m: m.text == "📞 Контакты")
def btn_contacts(message):
    bot.send_message(
        message.chat.id,
        "📞 *Контакты:*\n\n"
        "Менеджер: @ВАШ_ЮЗЕРНЕЙМ\n"        # ← замените
        "Рабочее время: 9:00 – 21:00\n\n"
        "Или просто напишите сюда — отвечу быстро! 🔥",
        parse_mode="Markdown"
    )

# ============================================================
#  НАПИСАТЬ МЕНЕДЖЕРУ
# ============================================================
@bot.message_handler(func=lambda m: m.text == "💬 Написать менеджеру")
def btn_write(message):
    bot.send_message(message.chat.id, "✍️ Напишите ваш вопрос — отвечу прямо сейчас 👇")

# ============================================================
#  ГЛАВНОЕ МЕНЮ (из админки)
# ============================================================
@bot.message_handler(func=lambda m: m.text == "🏠 Главное меню")
def btn_home(message):
    bot.send_message(message.chat.id, "🏠 Главное меню", reply_markup=main_menu())

# ============================================================
#  ДОБАВИТЬ ТОВАР (шаги)
# ============================================================
@bot.message_handler(func=lambda m: m.text == "➕ Добавить товар")
def btn_add(message):
    if not is_admin(message): return
    user_state[message.chat.id] = {"step": "add_name"}
    bot.send_message(message.chat.id, "📝 Введите *название* товара:", parse_mode="Markdown")

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_name")
def step_add_name(message):
    user_state[message.chat.id]["name"] = message.text.strip()
    user_state[message.chat.id]["step"] = "add_price"
    bot.send_message(message.chat.id, "💰 Введите *цену* (только цифры, например 1500):", parse_mode="Markdown")

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_price")
def step_add_price(message):
    try:
        price = float(message.text.replace(",", ".").strip())
        user_state[message.chat.id]["price"] = price
        user_state[message.chat.id]["step"] = "add_stock"
        bot.send_message(message.chat.id, "📦 Введите *количество* в наличии:", parse_mode="Markdown")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Нужно число. Например: 1500")

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_stock")
def step_add_stock(message):
    try:
        stock = int(message.text.strip())
        d = user_state[message.chat.id]
        key = d["name"].lower()
        products[key] = {"name": d["name"], "price": d["price"], "stock": stock}
        clear_state(message.chat.id)
        save_data()
        bot.send_message(message.chat.id, f"✅ Товар *{d['name']}* добавлен!", parse_mode="Markdown")
    except ValueError:
        bot.send_message(message.chat.id, "❌ Нужно целое число. Например: 10")

# ============================================================
#  УДАЛИТЬ ТОВАР
# ============================================================
@bot.message_handler(func=lambda m: m.text == "❌ Удалить товар")
def btn_delete(message):
    if not is_admin(message): return
    if not products:
        bot.send_message(message.chat.id, "❌ Товаров нет")
        return
    names = "\n".join(f"• {v['name']}" for v in products.values())
    user_state[message.chat.id] = {"step": "delete_name"}
    bot.send_message(message.chat.id, f"Текущие товары:\n{names}\n\n🗑 Введите название для удаления:")

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "delete_name")
def step_delete(message):
    key = message.text.strip().lower()
    if key in products:
        name = products[key]["name"]
        del products[key]
        clear_state(message.chat.id)
        save_data()
        bot.send_message(message.chat.id, f"🗑 *{name}* удалён", parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, "❌ Не нашёл. Проверьте написание.")

# ============================================================
#  ПРОСМОТР ТОВАРОВ (АДМИН)
# ============================================================
@bot.message_handler(func=lambda m: m.text == "📊 Все товары")
def btn_all_products(message):
    if not is_admin(message): return
    if not products:
        bot.send_message(message.chat.id, "❌ Товаров нет")
        return
    text = "📊 *Все товары:*\n\n"
    for item in products.values():
        text += f"• *{item['name']}* — {item['price']}$ | склад: {item['stock']} шт.\n"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# ============================================================
#  ЛИДЫ (АДМИН)
# ============================================================
@bot.message_handler(func=lambda m: m.text == "📈 Лиды")
def btn_leads(message):
    if not is_admin(message): return
    _show_leads(message)

def _show_leads(message):
    if not crm:
        bot.send_message(message.chat.id, "📭 Лидов пока нет")
        return

    rank = {"🔥 горячий": 0, "🌡 тёплый": 1, "❄️ холодный": 2}
    sorted_leads = sorted(crm.items(), key=lambda x: rank.get(x[1].get("status", ""), 3))

    text = "📊 *CRM — Лиды:*\n\n"
    for uid, d in sorted_leads[:25]:
        name    = d.get("name", "?")
        uname   = f"@{d['username']}" if d.get("username") else ""
        status  = d.get("status", "—")
        n_msgs  = len([h for h in d.get("history", []) if h["role"] == "user"])
        user_hs = [h for h in d.get("history", []) if h["role"] == "user"]
        last    = f'\n   💬 _{user_hs[-1]["text"][:45]}..._' if user_hs else ""
        text   += f"{status} — *{name}* {uname}\n   ID: `{uid}` | сообщений: {n_msgs}{last}\n\n"

    if len(crm) > 25:
        text += f"_...и ещё {len(crm) - 25} лидов_"

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

# ============================================================
#  AI-ПРОДАВЕЦ — основной обработчик всех текстовых сообщений
# ============================================================
ADMIN_BTN_STEPS = {"add_name", "add_price", "add_stock", "delete_name"}

@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def ai_seller(message):
    # Если идёт пошаговый ввод — не перехватываем
    if get_step(message.chat.id) in ADMIN_BTN_STEPS:
        return

    cancel_follow_up(message.chat.id)   # Клиент написал — дожим отменён
    status = detect_status(message.text)
    text   = message.text.lower()

    # ── 1. Упоминание конкретного товара ──
    for key, item in products.items():
        if key in text:
            avail = (f"✅ В наличии: {item['stock']} шт." if item["stock"] > 0
                     else "❌ Сейчас нет в наличии, но могу записать вас в лист ожидания")
            reply = (
                f"🔥 *{item['name']}* — всего *{item['price']}$*\n\n"
                f"{avail}\n\n"
                "Оформляем? Напишите *«оформить»* или задайте любой вопрос 👇"
            )
            save_lead(message, "🌡 тёплый", reply)
            bot.send_message(message.chat.id, reply, parse_mode="Markdown")
            if status == "🔥 горячий":
                notify_admin_hot(message)
            start_follow_up(message.chat.id)
            return

    # ── 2. Клиент готов купить ──
    if any(w in text for w in ["оформить", "беру", "заказать", "хочу купить", "да, оформляем"]):
        reply = (
            "✅ *Отлично!* Ваш запрос принят.\n\n"
            "Менеджер свяжется с вами в ближайшее время!\n"
            "Напишите удобное время для связи 📞"
        )
        save_lead(message, "🔥 горячий", reply)
        notify_admin_hot(message)
        bot.send_message(message.chat.id, reply, parse_mode="Markdown")
        return

    # ── 3. AI-ответ ──
    try:
        answer = ask_ai(message.chat.id, message.text)
        save_lead(message, status, answer)
        bot.send_message(message.chat.id, answer)
    except Exception as e:
        print(f"[AI ERROR] {e}")
        answer = "💬 Понял вас! Уточните, что именно вас интересует — подберу лучший вариант 👇"
        save_lead(message, status, answer)
        bot.send_message(message.chat.id, answer)

    if status == "🔥 горячий":
        notify_admin_hot(message)

    start_follow_up(message.chat.id)

# ============================================================
#  WEBHOOK
# ============================================================
@app.route("/webhook", methods=["POST"])
def webhook_handler():
    update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return "ok"

@app.route("/")
def health():
    return "Bot is running ✅"

# ============================================================
#  ЗАПУСК
# ============================================================
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=BASE_URL + "/webhook")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
