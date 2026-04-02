# ================================================================
#  AI SALES BOT — финальная версия
#  Деплой: Railway | Один файл | Без сложных зависимостей
# ================================================================
import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading

# ================================================================
#  КОНФИГУРАЦИЯ — переменные окружения Railway
# ================================================================
TOKEN         = os.environ.get("ADMIN_BOT_TOKEN")
ADMIN_ID      = int(os.environ.get("ADMIN_ID", "0"))
YANDEX_KEY    = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER = os.environ.get("YANDEX_FOLDER_ID")
BASE_URL      = os.environ.get("BASE_URL")   # https://ваш-app.up.railway.app

# ================================================================
#  ИНИЦИАЛИЗАЦИЯ
# ================================================================
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ================================================================
#  БАЗА ДАННЫХ (JSON-файл, потокобезопасная запись)
# ================================================================
DATA_FILE = "db.json"
_db_lock  = threading.Lock()

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"products": {}, "crm": {}}

def save_data():
    """Потокобезопасное сохранение."""
    with _db_lock:
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"products": products, "crm": crm},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[SAVE ERROR] {e}")

_raw     = load_data()
products = _raw.get("products", {})   # { key: {name, price, stock} }
crm      = _raw.get("crm", {})        # { uid: {name, username, status, history, created_at} }

# ================================================================
#  СОСТОЯНИЕ ДИАЛОГА И FOLLOW-UP
# ================================================================
user_state      = {}   # { chat_id: {"step": "...", ...} }
follow_up_events = {}  # { chat_id: threading.Event }

def get_step(chat_id):
    return user_state.get(chat_id, {}).get("step")

def clear_state(chat_id):
    user_state.pop(chat_id, None)

def is_admin(message):
    return message.chat.id == ADMIN_ID

# ================================================================
#  FOLLOW-UP — правильная реализация без дублей и без навязчивости
# ================================================================
def cancel_follow_up(chat_id):
    """Отменяет активный дожим для пользователя."""
    ev = follow_up_events.get(chat_id)
    if ev:
        ev.set()

def start_follow_up(chat_id):
    """
    Запускает серию follow-up сообщений.
    Если дожим уже был — отменяет старый и стартует новый.
    """
    cancel_follow_up(chat_id)          # остановить предыдущий поток
    ev = threading.Event()
    follow_up_events[chat_id] = ev
    t = threading.Thread(target=_follow_up_worker, args=(chat_id, ev), daemon=True)
    t.start()

def _follow_up_worker(chat_id, stop_event: threading.Event):
    """
    Три касания с нарастающим интервалом.
    event.wait(timeout) — ждёт N секунд ИЛИ немедленно возвращает True, если отменён.
    """
    schedule = [
        (3 * 60,  "💬 Остались вопросы? Готов помочь с выбором 👍"),
        (7 * 60,  "🔥 Кстати, есть варианты под ваш запрос. Показать?"),
        (15 * 60, "🎁 Последнее сообщение на сегодня — есть выгодное предложение. Напишите «да»!"),
    ]
    for delay, text in schedule:
        cancelled = stop_event.wait(timeout=delay)   # ждёт delay секунд
        if cancelled:
            return                                    # клиент написал — выходим
        try:
            bot.send_message(chat_id, text)
        except Exception:
            return

# ================================================================
#  AI — ЯНДЕКС GPT (с определением статуса лида через тег)
# ================================================================
def ask_ai(chat_id: int, user_text: str) -> tuple[str, str]:
    """
    Возвращает (текст_ответа, статус).
    GPT сам определяет статус и пишет [STATUS:HOT/WARM/COLD] в конце.
    Мы парсим тег регуляркой и убираем его из ответа клиенту.
    """
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    # Каталог для контекста
    catalog_text = ""
    if products:
        catalog_text = "\nКАТАЛОГ ТОВАРОВ:\n"
        for item in products.values():
            avail = f"в наличии {item['stock']} шт." if item["stock"] > 0 else "нет в наличии"
            catalog_text += f"- {item['name']}: {item['price']}$ ({avail})\n"

    system = (
        "Ты — профессиональный менеджер по продажам. Твои задачи:\n"
        "1. Выяснить потребность клиента\n"
        "2. Предложить подходящий товар из каталога\n"
        "3. Мягко подтолкнуть к покупке\n\n"
        "Правила:\n"
        "- Отвечай коротко: 2–4 предложения\n"
        "- Пиши по-человечески, уверенно, дружелюбно\n"
        "- Используй эмодзи умеренно (🔥 ✅ 👍)\n"
        "- Не выдумывай товары, которых нет в каталоге\n"
        "- Если клиент готов купить — предложи написать «оформить»\n\n"
        "ВАЖНО: В самом конце ответа — на отдельной строке — выведи ТОЛЬКО тег статуса:\n"
        "[STATUS:HOT] — если клиент готов купить\n"
        "[STATUS:WARM] — если интересуется, задаёт вопросы\n"
        "[STATUS:COLD] — если пришёл просто поглазеть\n"
        + catalog_text
    )

    # История последних 6 реплик
    uid = str(chat_id)
    history = []
    if uid in crm:
        for h in crm[uid].get("history", [])[-6:]:
            role = "user" if h["role"] == "user" else "assistant"
            history.append({"role": role, "text": h["text"]})

    messages = [{"role": "system", "text": system}]
    messages.extend(history)
    messages.append({"role": "user", "text": user_text})

    headers = {
        "Authorization": f"Api-Key {YANDEX_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER}/yandexgpt-lite",
        "completionOptions": {"temperature": 0.65, "maxTokens": 350},
        "messages": messages
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    raw = resp.json()["result"]["alternatives"][0]["message"]["text"]

    # Парсим статус
    status_map = {"HOT": "🔥 горячий", "WARM": "🌡 тёплый", "COLD": "❄️ холодный"}
    status = "❄️ холодный"
    match = re.search(r"\[STATUS:(HOT|WARM|COLD)\]", raw, re.IGNORECASE)
    if match:
        status = status_map.get(match.group(1).upper(), "❄️ холодный")

    clean = re.sub(r"\[STATUS:.*?\]", "", raw, flags=re.IGNORECASE).strip()
    return clean, status

# ================================================================
#  CRM — сохранение и уведомления
# ================================================================
_status_rank = {"🔥 горячий": 2, "🌡 тёплый": 1, "❄️ холодный": 0}

def save_lead(message, status: str, ai_reply: str = None):
    uid  = str(message.chat.id)
    name = (message.from_user.first_name or "Без имени")[:30]
    uname = message.from_user.username or ""

    if uid not in crm:
        crm[uid] = {
            "name": name,
            "username": uname,
            "status": status,
            "history": [],
            "created_at": int(time.time())
        }

    # Статус только повышается
    old_rank = _status_rank.get(crm[uid].get("status", ""), 0)
    new_rank = _status_rank.get(status, 0)
    if new_rank > old_rank:
        crm[uid]["status"] = status

    crm[uid]["history"].append({
        "role": "user",
        "text": message.text[:500],
        "ts": int(time.time())
    })
    if ai_reply:
        crm[uid]["history"].append({
            "role": "assistant",
            "text": ai_reply[:500],
            "ts": int(time.time())
        })
    save_data()

def notify_admin_hot(message):
    try:
        name  = message.from_user.first_name or "Без имени"
        uname = f"@{message.from_user.username}" if message.from_user.username else "нет username"
        uid   = message.chat.id
        bot.send_message(
            ADMIN_ID,
            f"🔥 ГОРЯЧИЙ ЛИД!\n\n"
            f"👤 {name} ({uname})\n"
            f"🆔 {uid}\n"
            f"💬 {message.text}\n\n"
            f"Написать: tg://user?id={uid}"
        )
    except Exception as e:
        print(f"[NOTIFY ERROR] {e}")

# ================================================================
#  КЛАВИАТУРЫ
# ================================================================
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("📦 Каталог", "💬 Написать менеджеру")
    kb.add("❓ Помощь",  "📞 Контакты")
    return kb

def admin_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("➕ Добавить товар", "❌ Удалить товар")
    kb.add("📊 Все товары",    "📈 Лиды")
    kb.add("📢 Рассылка",      "🏠 Главное меню")
    return kb

def cancel_kb():
    """Кнопка отмены во время многошаговых операций."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("❌ Отмена")
    return kb

# ================================================================
#  ШАГИ, КОТОРЫЕ НЕЛЬЗЯ ПЕРЕХВАТЫВАТЬ ОБЫЧНЫМИ ХЕНДЛЕРАМИ
# ================================================================
ADMIN_STEPS = {"add_name", "add_price", "add_stock", "delete_name", "broadcast_text"}

# ================================================================
#  /start
# ================================================================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    clear_state(message.chat.id)
    name = message.from_user.first_name or "друг"
    bot.send_message(
        message.chat.id,
        f"👋 Привет, {name}!\n\n"
        "Помогу подобрать лучшее предложение.\n"
        "Выберите действие 👇",
        reply_markup=main_menu()
    )

# ================================================================
#  /admin
# ================================================================
@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message):
        bot.send_message(message.chat.id, "⛔ Нет доступа")
        return
    clear_state(message.chat.id)
    bot.send_message(message.chat.id, "⚙️ Панель управления", reply_markup=admin_menu())

# ================================================================
#  /leads
# ================================================================
@bot.message_handler(commands=["leads"])
def cmd_leads(message):
    if not is_admin(message):
        return
    _show_leads(message)

# ================================================================
#  ОТМЕНА — работает в любом состоянии
# ================================================================
@bot.message_handler(func=lambda m: m.text == "❌ Отмена")
def btn_cancel(message):
    clear_state(message.chat.id)
    if is_admin(message):
        bot.send_message(message.chat.id, "↩️ Отменено", reply_markup=admin_menu())
    else:
        bot.send_message(message.chat.id, "↩️ Отменено", reply_markup=main_menu())

# ================================================================
#  КАТАЛОГ (клиент)
# ================================================================
@bot.message_handler(func=lambda m: m.text == "📦 Каталог")
def btn_catalog(message):
    clear_state(message.chat.id)
    if not products:
        bot.send_message(message.chat.id, "😔 Каталог пока пустой. Напишите — подберём под ваш запрос!")
        return
    text = "📦 Наш каталог:\n\n"
    for item in products.values():
        avail = f"✅ В наличии: {item['stock']} шт." if item["stock"] > 0 else "❌ Нет в наличии"
        text += f"{item['name']}\n💰 {item['price']}$\n{avail}\n\n"
    text += "Напишите название товара — расскажу подробнее 👇"
    bot.send_message(message.chat.id, text)

# ================================================================
#  НАПИСАТЬ МЕНЕДЖЕРУ
# ================================================================
@bot.message_handler(func=lambda m: m.text == "💬 Написать менеджеру")
def btn_write(message):
    clear_state(message.chat.id)
    bot.send_message(message.chat.id, "✍️ Напишите ваш вопрос — отвечу прямо сейчас 👇")

# ================================================================
#  ПОМОЩЬ
# ================================================================
@bot.message_handler(func=lambda m: m.text == "❓ Помощь")
def btn_help(message):
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        "🤝 Просто напишите:\n\n"
        "• Что вас интересует\n"
        "• Какой у вас бюджет\n"
        "• Для каких задач нужен товар\n\n"
        "И я подберу лучший вариант 👇"
    )

# ================================================================
#  КОНТАКТЫ
# ================================================================
@bot.message_handler(func=lambda m: m.text == "📞 Контакты")
def btn_contacts(message):
    clear_state(message.chat.id)
    bot.send_message(
        message.chat.id,
        "📞 Контакты:\n\n"
        "Менеджер: @ВАШ_ЮЗЕРНЕЙМ\n"    # ← замените
        "Рабочее время: 9:00 – 21:00\n\n"
        "Или просто напишите сюда — отвечу быстро! 🔥"
    )

# ================================================================
#  ГЛАВНОЕ МЕНЮ (из админки)
# ================================================================
@bot.message_handler(func=lambda m: m.text == "🏠 Главное меню")
def btn_home(message):
    clear_state(message.chat.id)
    bot.send_message(message.chat.id, "🏠 Главное меню", reply_markup=main_menu())

# ================================================================
#  ДОБАВИТЬ ТОВАР — многошаговый ввод
# ================================================================
@bot.message_handler(func=lambda m: m.text == "➕ Добавить товар")
def btn_add(message):
    if not is_admin(message): return
    user_state[message.chat.id] = {"step": "add_name"}
    bot.send_message(message.chat.id, "📝 Введите название товара:", reply_markup=cancel_kb())

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_name")
def step_add_name(message):
    if message.text == "❌ Отмена":
        return  # обработает btn_cancel выше
    user_state[message.chat.id]["name"] = message.text.strip()
    user_state[message.chat.id]["step"] = "add_price"
    bot.send_message(message.chat.id, "💰 Введите цену (только цифры, например 1500):", reply_markup=cancel_kb())

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_price")
def step_add_price(message):
    if message.text == "❌ Отмена":
        return
    try:
        price = float(message.text.replace(",", ".").strip())
        user_state[message.chat.id]["price"] = price
        user_state[message.chat.id]["step"] = "add_stock"
        bot.send_message(message.chat.id, "📦 Введите количество в наличии:", reply_markup=cancel_kb())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введите число. Например: 1500")

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "add_stock")
def step_add_stock(message):
    if message.text == "❌ Отмена":
        return
    try:
        stock = int(message.text.strip())
        d = user_state[message.chat.id]
        key = d["name"].lower()
        products[key] = {"name": d["name"], "price": d["price"], "stock": stock}
        clear_state(message.chat.id)
        save_data()
        bot.send_message(message.chat.id, f"✅ Товар «{d['name']}» добавлен!", reply_markup=admin_menu())
    except ValueError:
        bot.send_message(message.chat.id, "❌ Введите целое число. Например: 10")

# ================================================================
#  УДАЛИТЬ ТОВАР
# ================================================================
@bot.message_handler(func=lambda m: m.text == "❌ Удалить товар")
def btn_delete(message):
    if not is_admin(message): return
    if not products:
        bot.send_message(message.chat.id, "❌ Товаров нет")
        return
    names = "\n".join(f"• {v['name']}" for v in products.values())
    user_state[message.chat.id] = {"step": "delete_name"}
    bot.send_message(message.chat.id, f"Текущие товары:\n{names}\n\nВведите название для удаления:", reply_markup=cancel_kb())

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "delete_name")
def step_delete(message):
    if message.text == "❌ Отмена":
        return
    key = message.text.strip().lower()
    if key in products:
        name = products[key]["name"]
        del products[key]
        clear_state(message.chat.id)
        save_data()
        bot.send_message(message.chat.id, f"🗑 «{name}» удалён", reply_markup=admin_menu())
    else:
        bot.send_message(message.chat.id, "❌ Товар не найден. Проверьте написание или нажмите «❌ Отмена».")

# ================================================================
#  ВСЕ ТОВАРЫ (АДМИН)
# ================================================================
@bot.message_handler(func=lambda m: m.text == "📊 Все товары")
def btn_all_products(message):
    if not is_admin(message): return
    if not products:
        bot.send_message(message.chat.id, "❌ Товаров нет")
        return
    text = "📊 Все товары:\n\n"
    for item in products.values():
        text += f"• {item['name']} — {item['price']}$ | склад: {item['stock']} шт.\n"
    bot.send_message(message.chat.id, text)

# ================================================================
#  ЛИДЫ (АДМИН) — кнопка и команда
# ================================================================
@bot.message_handler(func=lambda m: m.text == "📈 Лиды")
def btn_leads(message):
    if not is_admin(message):
        return
    _show_leads(message)

def _show_leads(message):
    if not crm:
        bot.send_message(message.chat.id, "📭 Лидов пока нет")
        return

    rank = {"🔥 горячий": 0, "🌡 тёплый": 1, "❄️ холодный": 2}
    sorted_leads = sorted(crm.items(), key=lambda x: rank.get(x[1].get("status", ""), 3))

    text = "📊 CRM — Лиды:\n\n"
    for uid, d in sorted_leads[:20]:
        name   = d.get("name", "?")
        uname  = f"@{d['username']}" if d.get("username") else ""
        status = d.get("status", "—")
        msgs   = [h for h in d.get("history", []) if h["role"] == "user"]
        count  = len(msgs)
        last   = f"\n   Последнее: {msgs[-1]['text'][:40]}..." if msgs else ""
        text  += f"{status} — {name} {uname}\n   ID: {uid} | сообщений: {count}{last}\n\n"

    if len(crm) > 20:
        text += f"...и ещё {len(crm) - 20} лидов"

    # Разбиваем на части если текст очень длинный
    for i in range(0, len(text), 4000):
        bot.send_message(message.chat.id, text[i:i+4000])

# ================================================================
#  РАССЫЛКА (АДМИН)
# ================================================================
@bot.message_handler(func=lambda m: m.text == "📢 Рассылка")
def btn_broadcast(message):
    if not is_admin(message): return
    user_state[message.chat.id] = {"step": "broadcast_text"}
    bot.send_message(message.chat.id, "📢 Введите текст рассылки (отправится всем клиентам из CRM):", reply_markup=cancel_kb())

@bot.message_handler(func=lambda m: get_step(m.chat.id) == "broadcast_text")
def step_broadcast(message):
    if message.text == "❌ Отмена":
        return
    text = message.text
    clear_state(message.chat.id)

    sent, failed = 0, 0
    for uid in crm:
        try:
            bot.send_message(int(uid), f"📢 {text}")
            sent += 1
            time.sleep(0.05)   # антифлуд Telegram
        except Exception:
            failed += 1

    bot.send_message(message.chat.id,
                     f"✅ Рассылка завершена\nОтправлено: {sent}\nОшибок: {failed}",
                     reply_markup=admin_menu())

# ================================================================
#  AI-ПРОДАВЕЦ — главный обработчик всех текстовых сообщений
# ================================================================
@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def ai_seller(message):
    # Пропускаем если идёт пошаговый ввод для админки
    if get_step(message.chat.id) in ADMIN_STEPS:
        return

    cancel_follow_up(message.chat.id)  # новое сообщение — дожим сбрасывается
    text_lower = message.text.lower()

    # ── 1. Клиент упомянул конкретный товар ──
    for key, item in products.items():
        if key in text_lower:
            avail = (f"✅ В наличии: {item['stock']} шт."
                     if item["stock"] > 0
                     else "❌ Сейчас нет в наличии, но могу записать вас в список ожидания")
            reply = (
                f"🔥 {item['name']} — всего {item['price']}$\n\n"
                f"{avail}\n\n"
                "Оформляем? Напишите «оформить» или задайте любой вопрос 👇"
            )
            save_lead(message, "🌡 тёплый", reply)
            bot.send_message(message.chat.id, reply)
            start_follow_up(message.chat.id)
            return

    # ── 2. Клиент готов купить ──
    buy_triggers = ["оформить", "беру", "заказать", "хочу купить", "да, оформляем", "оплатить"]
    if any(w in text_lower for w in buy_triggers):
        reply = (
            "✅ Отлично! Ваш запрос принят.\n\n"
            "Менеджер свяжется с вами в ближайшее время!\n"
            "Напишите удобное время для связи 📞"
        )
        save_lead(message, "🔥 горячий", reply)
        notify_admin_hot(message)
        bot.send_message(message.chat.id, reply)
        return

    # ── 3. AI-ответ ──
    try:
        answer, status = ask_ai(message.chat.id, message.text)
        save_lead(message, status, answer)
        bot.send_message(message.chat.id, answer)

        if status == "🔥 горячий":
            notify_admin_hot(message)

    except Exception as e:
        print(f"[AI ERROR] {e}")
        fallback = "💬 Понял вас! Уточните, что именно вас интересует — подберу лучший вариант 👇"
        save_lead(message, "❄️ холодный", fallback)
        bot.send_message(message.chat.id, fallback)

    start_follow_up(message.chat.id)

# ================================================================
#  WEBHOOK
# ================================================================
@app.route("/webhook", methods=["POST"])
def webhook_handler():
    try:
        update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        print(f"[WEBHOOK ERROR] {e}")
    return "ok"

@app.route("/")
def health():
    return "Bot is running OK"

# ================================================================
#  ЗАПУСК
# ================================================================
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=BASE_URL + "/webhook")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
