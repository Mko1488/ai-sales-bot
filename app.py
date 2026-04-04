import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading, logging, io
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
# LOGGING  [STABLE]
# ═══════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("SaasBot")

# ═══════════════════════════════════════════════════════════════
# ENV
# ═══════════════════════════════════════════════════════════════
ADMIN_TOKEN      = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
YANDEX_KEY       = os.getenv("YANDEX_API_KEY")
YANDEX_FID       = os.getenv("YANDEX_FOLDER_ID")
BASE_URL         = os.getenv("BASE_URL")           # https://your-app.onrender.com
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "")

app    = Flask(__name__)
bots   = {}    # token -> TeleBot instance
owners = {}    # token -> owner_id

BOTS_FILE = "bots.json"

# ═══════════════════════════════════════════════════════════════
# FILE STORAGE
# ═══════════════════════════════════════════════════════════════
def load_bots() -> dict:
    """Загружает реестр подключённых ботов."""
    if os.path.exists(BOTS_FILE):
        try:
            with open(BOTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки {BOTS_FILE}: {e}")
    return {}

def save_bots(data: dict):
    """Атомарное сохранение реестра через tmp-файл."""
    tmp = BOTS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BOTS_FILE)
    except Exception as e:
        logger.error(f"Ошибка сохранения ботов: {e}")

# ═══════════════════════════════════════════════════════════════
# GOOGLE SHEETS  [SHEETS]  retry x3 + расширенные поля
# ═══════════════════════════════════════════════════════════════
def save_to_sheet(bot_token: str, user_id: str, name: str,
                  username: str, status: str, message: str,
                  summary: str = ""):
    """
    Отправка лида в Google Sheets.
    Вызывается асинхронно через threading.Thread.
    Retry 3 раза с нарастающей паузой.
    """
    if not GOOGLE_SHEET_URL:
        return
    payload = {
        "bot_token": bot_token[:8],
        "user_id":   user_id,
        "name":      name,
        "username":  username,
        "status":    status,
        "message":   message[:500],
        "summary":   summary[:300],
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    }
    for attempt in range(3):
        try:
            resp = requests.post(
                GOOGLE_SHEET_URL,
                json=payload,
                timeout=8,
                headers={"Content-Type": "application/json"}
            )
            if resp.status_code == 200:
                logger.info(f"[SHEETS] Записан лид {user_id}, статус={status}")
                return
            else:
                logger.warning(f"[SHEETS] HTTP {resp.status_code}, попытка {attempt + 1}")
        except requests.exceptions.Timeout:
            logger.warning(f"[SHEETS] Timeout, попытка {attempt + 1}")
        except Exception as e:
            logger.error(f"[SHEETS] Ошибка: {e}")
            return
        time.sleep(1.5 * (attempt + 1))
    logger.error(f"[SHEETS] Не удалось записать лид {user_id} после 3 попыток")

# ═══════════════════════════════════════════════════════════════
# CREATE BOT  — изолированный экземпляр [SAAS]
# ═══════════════════════════════════════════════════════════════
def create_bot(token: str, owner_id: int):
    """
    Создаёт полностью изолированный экземпляр бота.
    Каждый бот имеет свой файл db_{token[:8]}.json
    со своей CRM, каталогом и настройками.
    """
    if token in bots:
        logger.info(f"Бот {token[:8]}... уже запущен, пропускаем")
        return

    bot = telebot.TeleBot(token, threaded=False)
    logger.info(f"[SAAS] Инициализация бота {token[:8]}...")

    DATA_FILE = f"db_{token[:8]}.json"
    lock = threading.Lock()

    # ── Загрузка / сохранение данных ────────────────────────
    def load_data() -> dict:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки {DATA_FILE}: {e}")
        return {"products": {}, "crm": {}, "settings": {}}

    def save_data():
        """Атомарная запись через tmp-файл, защита threading.Lock."""
        tmp = DATA_FILE + ".tmp"
        with lock:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(
                        {"products": products, "crm": crm, "settings": settings},
                        f, ensure_ascii=False, indent=2
                    )
                os.replace(tmp, DATA_FILE)
            except Exception as e:
                logger.error(f"Ошибка сохранения {DATA_FILE}: {e}")

    raw      = load_data()
    products = raw.get("products", {})   # {pid: {name, price, desc, photo_id}}
    crm      = raw.get("crm", {})        # {uid: {name, username, status, history, ...}}
    settings = raw.get("settings", {})  # {manager_contact: str}

    # In-memory состояния (сбрасываются при рестарте — это нормально)
    user_state   = {}   # chat_id -> строка состояния
    follow_flags = {}   # chat_id -> bool
    last_active  = {}   # chat_id -> float timestamp

    # ── Хелперы состояния ────────────────────────────────────
    def is_admin(msg) -> bool:
        return msg.chat.id == owner_id

    def get_state(chat_id) -> str:
        return user_state.get(chat_id) or ""

    def set_state(chat_id, state: str):
        user_state[chat_id] = state

    def clear_state(chat_id):
        user_state[chat_id] = None

    # ════════════════════════════════════════════════════════
    # AI  [AI]  — продажник с этапами продаж и историей
    # ════════════════════════════════════════════════════════
    def ask_ai(chat_id: int, user_text: str):
        """
        YandexGPT с системным промптом профессионального продавца.
        В контекст передаётся:
          - каталог товаров
          - досье клиента (summary)
          - последние 10 сообщений диалога
        AI определяет статус через [STATUS:XXX] в конце ответа.
        """
        uid       = str(chat_id)
        user_data = crm.get(uid, {})
        history   = user_data.get("history", [])[-10:]
        summary   = user_data.get("summary", "")

        catalog_text = ""
        if products:
            lines = [f"  • {p['name']} — {p['price']}"
                     for p in products.values()]
            catalog_text = "КАТАЛОГ ТОВАРОВ:\n" + "\n".join(lines) + "\n\n"

        summary_text = (f"КРАТКОЕ ДОСЬЕ НА КЛИЕНТА: {summary}\n\n"
                        if summary else "")

        system_prompt = (
            "Ты — профессиональный менеджер по продажам. Живой, человечный, без шаблонов.\n\n"
            f"{catalog_text}"
            f"{summary_text}"
            "ЭТАПЫ ПРОДАЖ (строго соблюдай порядок):\n"
            "1. ПОТРЕБНОСТЬ — сначала пойми, что нужно клиенту. Задай открытый вопрос.\n"
            "2. УТОЧНЕНИЕ — уточни детали: объём, сроки, бюджет (ненавязчиво).\n"
            "3. ПРЕДЛОЖЕНИЕ — только после понимания потребности предложи конкретный вариант.\n"
            "4. МЯГКОЕ ЗАКРЫТИЕ — если клиент готов, предложи оформить. Без давления.\n\n"
            "ЗАПРЕЩЕНО:\n"
            "- Говорить 'оформляем!' или 'купите!' в первых сообщениях\n"
            "- Давить, торопить, спамить\n"
            "- Отвечать длиннее 3-4 предложений\n"
            "- Использовать клише типа 'Отличный выбор!'\n\n"
            "СТИЛЬ: коротко, по-человечески, с заботой о клиенте.\n\n"
            "В КОНЦЕ КАЖДОГО ОТВЕТА (обязательно, на новой строке) добавь тег:\n"
            "[STATUS:HOT]  — клиент явно готов купить, спрашивает цену/оформление\n"
            "[STATUS:WARM] — клиент интересуется, задаёт вопросы\n"
            "[STATUS:COLD] — просто пишет, не проявляет интерес к покупке\n"
            "[STATUS:DEAL] — клиент сказал 'беру', 'оформляю', подтвердил покупку"
        )

        messages = [{"role": "system", "text": system_prompt}]
        for h in history:
            role = "user" if h["role"] == "user" else "assistant"
            messages.append({"role": role, "text": h["text"]})
        messages.append({"role": "user", "text": user_text})

        try:
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            resp = requests.post(
                url,
                headers={"Authorization": f"Api-Key {YANDEX_KEY}"},
                json={
                    "modelUri": f"gpt://{YANDEX_FID}/yandexgpt-lite",
                    "completionOptions": {
                        "temperature": 0.65,
                        "maxTokens":   350,
                        "stream":      False
                    },
                    "messages": messages
                },
                timeout=15
            )
            resp.raise_for_status()
            raw_text = resp.json()["result"]["alternatives"][0]["message"]["text"]

            status = "cold"
            m = re.search(r"\[STATUS:(HOT|WARM|COLD|DEAL)\]", raw_text, re.IGNORECASE)
            if m:
                status = m.group(1).lower()

            clean = re.sub(r"\[STATUS:.*?\]", "", raw_text, flags=re.IGNORECASE).strip()
            logger.info(f"[AI] chat={chat_id} status={status} len={len(clean)}")
            return clean, status

        except requests.exceptions.Timeout:
            logger.warning(f"[AI] Timeout для chat_id={chat_id}")
            return "Можешь чуть подробнее описать, что тебя интересует?", "cold"
        except Exception as e:
            logger.error(f"[AI] Ошибка: {e}")
            return "Расскажи подробнее, чем могу помочь 👇", "cold"

    # ════════════════════════════════════════════════════════
    # AI SUMMARY  [CRM]  — краткое досье клиента
    # ════════════════════════════════════════════════════════
    def generate_summary(uid: str) -> str:
        """Генерирует 1-2 предложения досье клиента по истории диалога."""
        history = crm.get(uid, {}).get("history", [])
        if len(history) < 3:
            return crm.get(uid, {}).get("summary", "")

        dialog = "\n".join([
            f"{'Клиент' if h['role'] == 'user' else 'Менеджер'}: {h['text']}"
            for h in history[-12:]
        ])
        prompt = (
            "На основе диалога составь краткое досье на клиента (1-2 предложения).\n"
            "Укажи: интерес, потребность, готовность к покупке. Без лишних слов.\n\n"
            f"ДИАЛОГ:\n{dialog}"
        )
        try:
            url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"
            resp = requests.post(
                url,
                headers={"Authorization": f"Api-Key {YANDEX_KEY}"},
                json={
                    "modelUri": f"gpt://{YANDEX_FID}/yandexgpt-lite",
                    "completionOptions": {"temperature": 0.3, "maxTokens": 120},
                    "messages": [{"role": "user", "text": prompt}]
                },
                timeout=10
            )
            resp.raise_for_status()
            return resp.json()["result"]["alternatives"][0]["message"]["text"].strip()
        except Exception as e:
            logger.warning(f"[CRM] Summary error: {e}")
            return crm.get(uid, {}).get("summary", "")

    def _update_summary_async(uid: str):
        """Асинхронно обновляет summary и сохраняет в файл."""
        try:
            new_summary = generate_summary(uid)
            if new_summary:
                with lock:
                    if uid in crm:
                        crm[uid]["summary"] = new_summary
                save_data()
                logger.info(f"[CRM] Summary обновлён для {uid}")
        except Exception as e:
            logger.error(f"[CRM] Summary async error: {e}")

    # ════════════════════════════════════════════════════════
    # CRM  [CRM]  — сохранение лида, история, уведомления
    # ════════════════════════════════════════════════════════
    def save_lead(msg, status: str, ai_reply: str = None):
        """
        Сохраняет / обновляет лид в CRM:
        - история до 30 записей (15 пар диалога)
        - summary обновляется каждые 5 сообщений
        - уведомление владельца только при повышении статуса
        - Google Sheets — асинхронно
        """
        uid      = str(msg.chat.id)
        name     = (msg.from_user.first_name or "").strip()
        username = (msg.from_user.username or "").strip()

        if uid not in crm:
            crm[uid] = {
                "name":       name,
                "username":   username,
                "status":     "cold",
                "history":    [],
                "summary":    "",
                "created_at": int(time.time()),
                "msg_count":  0
            }
            logger.info(f"[CRM] Новый лид: {name} @{username} ({uid})")

        prev_status = crm[uid].get("status", "cold")

        crm[uid]["name"]      = name or crm[uid]["name"]
        crm[uid]["username"]  = username or crm[uid]["username"]
        crm[uid]["status"]    = status
        crm[uid]["last_msg"]  = msg.text
        crm[uid]["last_ts"]   = int(time.time())
        crm[uid]["msg_count"] = crm[uid].get("msg_count", 0) + 1

        crm[uid]["history"].append({
            "role": "user",
            "text": msg.text,
            "ts":   int(time.time())
        })
        if ai_reply:
            crm[uid]["history"].append({
                "role": "assistant",
                "text": ai_reply,
                "ts":   int(time.time())
            })
        # Ограничиваем историю 30 записями
        crm[uid]["history"] = crm[uid]["history"][-30:]

        # Summary каждые 5 сообщений
        if crm[uid]["msg_count"] % 5 == 0:
            threading.Thread(
                target=lambda: _update_summary_async(uid),
                daemon=True
            ).start()

        save_data()

        # Google Sheets — асинхронно
        threading.Thread(
            target=save_to_sheet,
            args=(token, uid, name, username, status,
                  msg.text, crm[uid].get("summary", "")),
            daemon=True
        ).start()

        _notify_owner_if_needed(uid, name, username, msg.text,
                                status, prev_status)

    def _notify_owner_if_needed(uid, name, username, text,
                                status, prev_status):
        """Уведомляет владельца только при повышении статуса."""
        status_order = {"cold": 0, "warm": 1, "hot": 2, "deal": 3}
        if status_order.get(status, 0) <= status_order.get(prev_status, 0):
            return

        emoji = {"warm": "🌡", "hot": "🔥", "deal": "💰"}.get(status, "")
        if not emoji:
            return

        summary   = crm.get(uid, {}).get("summary", "")
        msg_count = crm.get(uid, {}).get("msg_count", 0)

        text_notify = (
            f"{emoji} *{status.upper()} ЛИД*\n"
            f"👤 {name} | @{username}\n"
            f"🆔 `{uid}`\n"
            f"💬 Сообщений: {msg_count}\n"
            f"📝 Последнее: {text[:200]}\n"
        )
        if summary:
            text_notify += f"📋 Досье: {summary}"

        try:
            bot.send_message(owner_id, text_notify, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"[CRM] Не удалось уведомить владельца: {e}")

    # ════════════════════════════════════════════════════════
    # FOLLOW-UP  [FOLLOW]  — умный, не спамит
    # ════════════════════════════════════════════════════════
    def follow(chat_id: int):
        """
        2 мягких касания: через 20 мин и через 40 мин.
        Перед каждым отправкой проверяет активность пользователя.
        Не беспокоит тех, кто недавно писал.
        """
        if follow_flags.get(chat_id):
            return
        follow_flags[chat_id] = True
        last_active[chat_id]  = time.time()

        def worker():
            # Касание 1 — через 20 минут
            time.sleep(1200)
            if not follow_flags.get(chat_id):
                return
            if time.time() - last_active.get(chat_id, 0) >= 900:
                try:
                    bot.send_message(
                        chat_id,
                        "Остались вопросы? Я тут 😊 Могу помочь с выбором"
                    )
                    logger.info(f"[FOLLOW] Follow-up 1 → {chat_id}")
                except Exception as e:
                    logger.warning(f"[FOLLOW] Ошибка 1: {e}")

            # Касание 2 — ещё через 40 минут
            time.sleep(2400)
            if not follow_flags.get(chat_id):
                return
            if time.time() - last_active.get(chat_id, 0) < 1800:
                return   # был активен — не беспокоим
            try:
                bot.send_message(
                    chat_id,
                    "Если что — пиши, помогу подобрать лучший вариант 🙌"
                )
                logger.info(f"[FOLLOW] Follow-up 2 → {chat_id}")
            except Exception as e:
                logger.warning(f"[FOLLOW] Ошибка 2: {e}")
            follow_flags[chat_id] = False

        threading.Thread(target=worker, daemon=True).start()

    def cancel_follow(chat_id: int):
        """Отменяет follow-up при любом новом сообщении от пользователя."""
        follow_flags[chat_id] = False
        last_active[chat_id]  = time.time()

    # ════════════════════════════════════════════════════════
    # UI  [ROLE_SEP]  — строгое разделение прав
    # ════════════════════════════════════════════════════════
    def main_menu(msg):
        """
        Покупатель: строго 2 кнопки — «📦 Каталог» и «💬 Написать менеджеру».
        Администратор (owner_id): дополнительно все кнопки управления.
        """
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        # Для всех пользователей
        kb.add("📦 Каталог", "💬 Написать менеджеру")
        # Только для владельца бота
        if is_admin(msg):
            kb.row("📊 Лиды", "📢 Рассылка")
            kb.row("➕ Добавить товар", "❌ Удалить товар")
            kb.row("📈 Статистика")
        return kb

    def admin_back_kb():
        """Клавиатура-возврат для многошаговых сценариев."""
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("🔙 Назад")
        return kb

    # ════════════════════════════════════════════════════════
    # HANDLERS
    # ПОРЯДОК РЕГИСТРАЦИИ КРИТИЧЕН:
    #   специфичные команды → кнопки → состояния → catch-all AI
    # ════════════════════════════════════════════════════════

    # ── /start ───────────────────────────────────────────────
    @bot.message_handler(commands=["start"])
    def start(msg):
        name = msg.from_user.first_name or "друг"
        bot.send_message(
            msg.chat.id,
            f"Привет, {name}! 👋\n\nЧем могу помочь?",
            reply_markup=main_menu(msg)
        )
        logger.info(f"[BOT] /start от {msg.chat.id}")

    # ── /cancel ──────────────────────────────────────────────
    @bot.message_handler(commands=["cancel"])
    def cancel_cmd(msg):
        clear_state(msg.chat.id)
        bot.send_message(msg.chat.id, "Отменено ✅", reply_markup=main_menu(msg))

    # ── /export  — CSV-выгрузка CRM ─────────────────────────
    @bot.message_handler(commands=["export"])
    def export_crm(msg):
        if not is_admin(msg):
            return
        try:
            lines = ["ID,Имя,Username,Статус,Сообщений,Последнее сообщение,Досье"]
            for uid, d in crm.items():
                lines.append(",".join([
                    uid,
                    (d.get("name") or "").replace(",", " "),
                    (d.get("username") or "").replace(",", " "),
                    d.get("status", "cold"),
                    str(d.get("msg_count", 0)),
                    (d.get("last_msg") or "").replace(",", " ").replace("\n", " ")[:100],
                    (d.get("summary") or "").replace(",", " ").replace("\n", " ")[:200]
                ]))
            csv_bytes = "\n".join(lines).encode("utf-8-sig")
            fname = f"crm_export_{token[:6]}_{int(time.time())}.csv"
            bot.send_document(
                msg.chat.id,
                (fname, io.BytesIO(csv_bytes)),
                caption=f"📊 Экспорт CRM: {len(crm)} лидов"
            )
        except Exception as e:
            logger.error(f"[ADMIN] Export error: {e}")
            bot.send_message(msg.chat.id, f"❌ Ошибка экспорта: {e}")

    # ── /setcontact — контакт живого менеджера ───────────────
    @bot.message_handler(commands=["setcontact"])
    def set_contact(msg):
        """
        Устанавливает контакт менеджера для кнопки «Написать менеджеру».
        Использование: /setcontact @username
        """
        if not is_admin(msg):
            return
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            bot.send_message(
                msg.chat.id,
                "Использование: `/setcontact @username_менеджера`",
                parse_mode="Markdown"
            )
            return
        contact = parts[1].strip()
        settings["manager_contact"] = contact
        save_data()
        bot.send_message(
            msg.chat.id,
            f"✅ Контакт менеджера установлен: *{contact}*\n\n"
            f"Теперь клиенты увидят его при нажатии «💬 Написать менеджеру».",
            parse_mode="Markdown"
        )
        logger.info(f"[ADMIN] Manager contact: {contact}")

    # ── 📦 Каталог  [PHOTO] ──────────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "📦 Каталог")
    def catalog(msg):
        """
        Выводит каталог товаров.
        У товаров с photo_id — send_photo с подписью.
        У товаров без фото — обычный текст (обратная совместимость).
        """
        if not products:
            bot.send_message(
                msg.chat.id,
                "Каталог пока пуст. Скоро добавим товары! 🔜"
            )
            return

        bot.send_message(
            msg.chat.id,
            f"📦 *Наш каталог* — {len(products)} товаров:",
            parse_mode="Markdown"
        )

        for p in products.values():
            name_part  = f"*{p['name']}*"
            price_part = f"Цена: {p['price']}"
            desc_part  = f"_{p['desc']}_" if p.get("desc") else ""
            caption    = "\n".join(filter(None, [name_part, price_part, desc_part]))

            try:
                if p.get("photo_id"):
                    bot.send_photo(
                        msg.chat.id,
                        p["photo_id"],
                        caption=caption,
                        parse_mode="Markdown"
                    )
                else:
                    bot.send_message(
                        msg.chat.id,
                        caption,
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.warning(f"[CATALOG] Ошибка товара {p.get('name')}: {e}")
                # Фоллбэк: если фото сломалось — выводим текстом
                try:
                    bot.send_message(msg.chat.id, caption, parse_mode="Markdown")
                except Exception:
                    pass

    # ── 💬 Написать менеджеру  [MANAGER] ────────────────────
    # ВАЖНО: зарегистрирован ДО handle_all (AI),
    # поэтому это сообщение НИКОГДА не попадёт в YandexGPT.
    @bot.message_handler(func=lambda m: m.text == "💬 Написать менеджеру")
    def contact_manager(msg):
        """
        Отдельный хендлер — AI не вызывается.
        Показывает контакт из settings['manager_contact']
        (устанавливается командой /setcontact).
        """
        contact = settings.get("manager_contact", "").strip()
        if contact:
            text = (
                f"Для связи с живым менеджером:\n"
                f"👤 {contact}\n\n"
                f"Напишите напрямую — ответим быстро! 🙌"
            )
        else:
            text = (
                "Для связи с живым менеджером напишите нам напрямую.\n"
                "Мы ответим в ближайшее время! 🙌"
            )
        bot.send_message(msg.chat.id, text)

    # ── 📊 Лиды  [ADMIN] ─────────────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "📊 Лиды")
    def leads(msg):
        if not is_admin(msg):
            return   # молча игнорируем — не передаём в AI

        if not crm:
            bot.send_message(msg.chat.id, "Лидов пока нет 🤷")
            return

        order = {"deal": 0, "hot": 1, "warm": 2, "cold": 3}
        sorted_leads = sorted(
            crm.items(),
            key=lambda x: (
                order.get(x[1].get("status", "cold"), 4),
                -(x[1].get("last_ts", 0))
            )
        )
        emoji_map = {"hot": "🔥", "warm": "🌡", "cold": "❄️", "deal": "💰"}

        bot.send_message(
            msg.chat.id,
            f"📊 *Всего лидов: {len(crm)}*\n"
            f"💰 Сделок: {sum(1 for d in crm.values() if d.get('status') == 'deal')} | "
            f"🔥 Горячих: {sum(1 for d in crm.values() if d.get('status') == 'hot')} | "
            f"🌡 Тёплых: {sum(1 for d in crm.values() if d.get('status') == 'warm')}",
            parse_mode="Markdown"
        )

        for uid, d in sorted_leads[:20]:
            status  = d.get("status", "cold")
            emoji   = emoji_map.get(status, "❓")
            name    = d.get("name", "—") or "—"
            uname   = f"@{d['username']}" if d.get("username") else "нет username"
            last    = d.get("last_msg", "—") or "—"
            summary = d.get("summary", "")
            count   = d.get("msg_count", 0)
            ts      = d.get("last_ts", 0)
            dt      = (datetime.utcfromtimestamp(ts).strftime("%d.%m %H:%M")
                       if ts else "—")

            text = (
                f"{emoji} *{name}* | {uname}\n"
                f"🆔 `{uid}`\n"
                f"📊 Статус: `{status.upper()}`\n"
                f"💬 Сообщений: {count} | Последнее: {dt}\n"
                f"📝 _{last[:150]}_"
            )
            if summary:
                text += f"\n📋 Досье: {summary[:200]}"

            try:
                bot.send_message(msg.chat.id, text, parse_mode="Markdown")
            except Exception as e:
                logger.warning(f"[ADMIN] Ошибка отправки лида {uid}: {e}")

        if len(crm) > 20:
            bot.send_message(
                msg.chat.id,
                f"_...и ещё {len(crm) - 20} лидов. Полный список: /export_",
                parse_mode="Markdown"
            )

    # ── 📈 Статистика  [ADMIN] ───────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "📈 Статистика")
    def stats(msg):
        if not is_admin(msg):
            return

        total = len(crm)
        cold  = sum(1 for d in crm.values() if d.get("status") == "cold")
        warm  = sum(1 for d in crm.values() if d.get("status") == "warm")
        hot   = sum(1 for d in crm.values() if d.get("status") == "hot")
        deal  = sum(1 for d in crm.values() if d.get("status") == "deal")

        day_ago    = time.time() - 86400
        active_day = sum(1 for d in crm.values()
                         if d.get("last_ts", 0) > day_ago)

        conv_rate = round(deal / total * 100, 1) if total else 0
        hot_rate  = round((hot + deal) / total * 100, 1) if total else 0

        bot.send_message(
            msg.chat.id,
            "📈 *СТАТИСТИКА CRM*\n\n"
            f"👥 Всего лидов: *{total}*\n"
            f"❄️ Cold: {cold}  🌡 Warm: {warm}  🔥 Hot: {hot}  💰 Deal: {deal}\n\n"
            f"🕐 Активных за 24ч: *{active_day}*\n"
            f"📊 Конверсия в сделку: *{conv_rate}%*\n"
            f"🎯 Hot rate: *{hot_rate}%*\n\n"
            f"📦 Товаров в каталоге: *{len(products)}*\n\n"
            f"_Полный экспорт: /export_",
            parse_mode="Markdown"
        )

    # ── 📢 Рассылка  [ADMIN] ─────────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "📢 Рассылка")
    def broadcast_start(msg):
        if not is_admin(msg):
            return
        set_state(msg.chat.id, "broadcast")
        bot.send_message(
            msg.chat.id,
            "Введите текст рассылки.\n"
            "⚠️ Будет отправлен всем пользователям из CRM.\n"
            "Для отмены — /cancel или кнопка 🔙 Назад",
            reply_markup=admin_back_kb()
        )

    # ── ➕ Добавить товар  [ADMIN] ────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "➕ Добавить товар")
    def add_product_start(msg):
        if not is_admin(msg):
            return
        set_state(msg.chat.id, "add_product_name")
        bot.send_message(
            msg.chat.id,
            "Введите *название* товара:",
            parse_mode="Markdown",
            reply_markup=admin_back_kb()
        )

    # ── ❌ Удалить товар  [DELETE] ────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "❌ Удалить товар")
    def delete_product_start(msg):
        """
        Показывает inline-клавиатуру со всеми товарами.
        Каждая кнопка — callback_data='del_{pid}'.
        """
        if not is_admin(msg):
            return
        if not products:
            bot.send_message(msg.chat.id, "Каталог пуст — нечего удалять.")
            return

        kb = types.InlineKeyboardMarkup(row_width=1)
        for pid, p in products.items():
            label = f"❌ {p['name']} — {p['price']}"
            kb.add(types.InlineKeyboardButton(
                text=label,
                callback_data=f"del_{pid}"
            ))
        bot.send_message(
            msg.chat.id,
            "Выберите товар для удаления:",
            reply_markup=kb
        )

    # ════════════════════════════════════════════════════════
    # STATE HANDLERS  — зарегистрированы до catch-all
    # ════════════════════════════════════════════════════════

    # ── Рассылка: получаем текст и отправляем ───────────────
    @bot.message_handler(func=lambda m: get_state(m.chat.id) == "broadcast")
    def broadcast_send(msg):
        if not is_admin(msg):
            return
        if msg.text == "🔙 Назад":
            clear_state(msg.chat.id)
            bot.send_message(msg.chat.id, "Отменено.", reply_markup=main_menu(msg))
            return

        sent = failed = 0
        for uid in list(crm.keys()):
            try:
                bot.send_message(int(uid), msg.text)
                sent += 1
                time.sleep(0.05)    # защита от флуда Telegram
            except Exception:
                failed += 1

        clear_state(msg.chat.id)
        bot.send_message(
            msg.chat.id,
            f"✅ Рассылка завершена\n"
            f"✔️ Доставлено: {sent}\n"
            f"❌ Ошибок: {failed}",
            reply_markup=main_menu(msg)
        )
        logger.info(f"[ADMIN] Рассылка: sent={sent}, failed={failed}")

    # ── Шаг 1: Название товара ───────────────────────────────
    @bot.message_handler(func=lambda m: get_state(m.chat.id) == "add_product_name")
    def add_product_name(msg):
        if not is_admin(msg):
            return
        if msg.text == "🔙 Назад":
            clear_state(msg.chat.id)
            bot.send_message(msg.chat.id, "Отменено.", reply_markup=main_menu(msg))
            return
        name = msg.text.strip()
        set_state(msg.chat.id, f"add_product_price|{name}")
        bot.send_message(
            msg.chat.id,
            f"Товар: *{name}*\n\nВведите *цену*:",
            parse_mode="Markdown"
        )

    # ── Шаг 2: Цена товара ───────────────────────────────────
    @bot.message_handler(
        func=lambda m: get_state(m.chat.id).startswith("add_product_price|")
    )
    def add_product_price(msg):
        if not is_admin(msg):
            return
        if msg.text == "🔙 Назад":
            set_state(msg.chat.id, "add_product_name")
            bot.send_message(msg.chat.id, "Введите название товара снова:")
            return

        state        = get_state(msg.chat.id)
        product_name = state.split("|", 1)[1]
        price        = msg.text.strip()

        set_state(msg.chat.id, f"add_product_photo|{product_name}|{price}")
        bot.send_message(
            msg.chat.id,
            f"Товар: *{product_name}* — {price}\n\n"
            f"📸 Отправьте *фото* товара.\n"
            f"Или напишите *пропустить* если фото нет.",
            parse_mode="Markdown"
        )

    # ── Шаг 3: Фото товара  [PHOTO] ──────────────────────────
    # content_types=['photo', 'text'] — принимаем и фото, и «пропустить»
    @bot.message_handler(
        content_types=["photo", "text"],
        func=lambda m: get_state(m.chat.id).startswith("add_product_photo|")
    )
    def add_product_photo(msg):
        if not is_admin(msg):
            return

        state = get_state(msg.chat.id)
        parts = state.split("|", 2)
        product_name = parts[1] if len(parts) > 1 else "Товар"
        price        = parts[2] if len(parts) > 2 else ""

        # Кнопка «Назад» — возврат к шагу цены
        if msg.content_type == "text" and msg.text == "🔙 Назад":
            set_state(msg.chat.id, f"add_product_price|{product_name}")
            bot.send_message(
                msg.chat.id,
                f"Товар: *{product_name}*\n\nВведите цену снова:",
                parse_mode="Markdown"
            )
            return

        photo_id = None

        if msg.content_type == "photo" and msg.photo:
            # Берём наибольшее разрешение — последний элемент массива
            photo_id = msg.photo[-1].file_id
            logger.info(f"[ADMIN] Фото получено: {photo_id[:20]}...")

        elif msg.content_type == "text":
            if msg.text and msg.text.lower() in ["пропустить", "skip", "-"]:
                pass   # продолжаем без фото
            else:
                bot.send_message(
                    msg.chat.id,
                    "Отправьте *фото* товара или напишите *пропустить*",
                    parse_mode="Markdown"
                )
                return   # остаёмся в этом состоянии

        # Сохраняем товар
        pid = str(int(time.time()))
        products[pid] = {
            "name":     product_name,
            "price":    price,
            "desc":     "",
            "photo_id": photo_id    # None если без фото
        }
        save_data()
        clear_state(msg.chat.id)

        result = (
            f"✅ Товар добавлен!\n\n"
            f"📦 *{product_name}*\n"
            f"💰 Цена: {price}\n"
            f"{'📸 Фото прикреплено' if photo_id else '_(без фото)_'}"
        )
        bot.send_message(
            msg.chat.id,
            result,
            parse_mode="Markdown",
            reply_markup=main_menu(msg)
        )
        logger.info(f"[ADMIN] Товар: {product_name}, фото={bool(photo_id)}")

    # ════════════════════════════════════════════════════════
    # CALLBACK: удаление товара  [DELETE]
    # ════════════════════════════════════════════════════════
    @bot.callback_query_handler(func=lambda c: c.data.startswith("del_"))
    def delete_product_confirm(call):
        """Обрабатывает нажатие inline-кнопки удаления товара."""
        if call.message.chat.id != owner_id:
            bot.answer_callback_query(call.id, "❌ Нет доступа")
            return

        pid = call.data[4:]   # убираем префикс «del_»

        if pid in products:
            name = products[pid]["name"]
            del products[pid]
            save_data()
            bot.answer_callback_query(call.id, f"✅ Удалено: {name}")
            try:
                bot.edit_message_text(
                    f"✅ Товар *«{name}»* удалён из каталога.",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            logger.info(f"[ADMIN] Товар удалён: {name}")
        else:
            bot.answer_callback_query(call.id, "Товар не найден")

    # ════════════════════════════════════════════════════════
    # AI HANDLER  — catch-all, ПОСЛЕДНИМ  [AI]
    #
    # Сюда попадают только обычные текстовые сообщения
    # пользователей. Все кнопки и состояния обработаны выше.
    # ════════════════════════════════════════════════════════
    @bot.message_handler(func=lambda m: True)
    def handle_all(msg):
        # Отменяем follow-up — пользователь активен
        cancel_follow(msg.chat.id)

        # Safety-net: если активно состояние — оно должно было
        # поймать сообщение раньше этого хендлера
        if get_state(msg.chat.id):
            return

        # Пустое сообщение — игнорируем
        if not msg.text or not msg.text.strip():
            return

        # Вызываем AI-продажника
        answer, status = ask_ai(msg.chat.id, msg.text)

        try:
            bot.send_message(msg.chat.id, answer)
        except Exception as e:
            logger.error(f"[BOT] Ошибка отправки ответа: {e}")
            return

        # Сохраняем лид с историей (и ответом AI)
        save_lead(msg, status, answer)

        # Запускаем умный follow-up
        follow(msg.chat.id)

    # ── Регистрируем бота ─────────────────────────────────────
    bots[token]   = bot
    owners[token] = owner_id

    data = load_bots()
    data[token] = owner_id
    save_bots(data)

    try:
        bot.remove_webhook()
        time.sleep(0.3)
        bot.set_webhook(url=f"{BASE_URL}/bot/{token}")
        logger.info(f"[SAAS] Webhook: {BASE_URL}/bot/{token[:8]}...")
    except Exception as e:
        logger.error(f"[SAAS] Ошибка webhook: {e}")


# ═══════════════════════════════════════════════════════════════
# ADMIN BOT  [ADMIN_MENU] [BTN_FIX]
#
# Отдельный бот для подключения клиентских ботов.
# Имеет собственное Reply-меню и систему состояний.
# Токен принимается ТОЛЬКО когда state == 'awaiting_token'
# (после нажатия «➕ Подключить бота»).
# Все кнопки меню обработаны до catch-all → кнопки не ломают бот.
# ═══════════════════════════════════════════════════════════════
admin_bot        = telebot.TeleBot(ADMIN_TOKEN, threaded=False)
admin_user_state = {}   # chat_id -> состояние admin-бота

def admin_main_menu():
    """Главное меню admin-бота."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add("➕ Подключить бота")
    kb.add("📋 Мои боты")
    return kb

def admin_cancel_kb():
    """Клавиатура с кнопкой отмены во время ввода токена."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🔙 Отмена")
    return kb

# ── /start ───────────────────────────────────────────────────
@admin_bot.message_handler(commands=["start"])
def admin_start(msg):
    admin_user_state[msg.chat.id] = None
    admin_bot.send_message(
        msg.chat.id,
        "🚀 *AI Sales Bot — Панель управления*\n\n"
        "Нажмите «➕ Подключить бота» чтобы добавить своего бота.\n\n"
        "Токен можно получить у @BotFather → /newbot",
        parse_mode="Markdown",
        reply_markup=admin_main_menu()
    )

# ── Кнопка «➕ Подключить бота» ──────────────────────────────
# Зарегистрирована ДО catch-all — перехватывается корректно
@admin_bot.message_handler(func=lambda m: m.text == "➕ Подключить бота")
def admin_connect_start(msg):
    """Переводит в состояние ожидания токена."""
    admin_user_state[msg.chat.id] = "awaiting_token"
    admin_bot.send_message(
        msg.chat.id,
        "Отправьте токен вашего бота.\n\n"
        "Формат: `1234567890:ABCdef...`",
        parse_mode="Markdown",
        reply_markup=admin_cancel_kb()
    )

# ── Кнопка «📋 Мои боты» ────────────────────────────────────
@admin_bot.message_handler(func=lambda m: m.text == "📋 Мои боты")
def admin_list_bots(msg):
    """Показывает список ботов, подключённых этим пользователем."""
    saved   = load_bots()
    my_bots = [(t, oid) for t, oid in saved.items() if oid == msg.chat.id]

    if not my_bots:
        admin_bot.send_message(
            msg.chat.id,
            "У вас нет подключённых ботов.\n"
            "Нажмите «➕ Подключить бота» чтобы добавить.",
            reply_markup=admin_main_menu()
        )
        return

    text = f"🤖 *Ваши боты ({len(my_bots)}):*\n\n"
    for t, _ in my_bots:
        text += f"• `{t[:10]}...`\n"
    admin_bot.send_message(
        msg.chat.id,
        text,
        parse_mode="Markdown",
        reply_markup=admin_main_menu()
    )

# ── Кнопка «🔙 Отмена» ──────────────────────────────────────
@admin_bot.message_handler(func=lambda m: m.text == "🔙 Отмена")
def admin_cancel(msg):
    """Отменяет ввод токена и возвращает в главное меню."""
    admin_user_state[msg.chat.id] = None
    admin_bot.send_message(
        msg.chat.id,
        "Отменено.",
        reply_markup=admin_main_menu()
    )

# ── Получение токена  (только в состоянии awaiting_token) ────
@admin_bot.message_handler(
    func=lambda m: admin_user_state.get(m.chat.id) == "awaiting_token"
)
def admin_connect_token(msg):
    """
    [BTN_FIX] Токен проверяется ТОЛЬКО здесь.
    Этот хендлер срабатывает лишь когда state == 'awaiting_token'.
    Кнопки «➕ Подключить бота», «📋 Мои боты», «🔙 Отмена»
    обработаны выше и сюда не доходят.
    Поэтому «Неверный формат токена» на кнопки больше не показывается.
    """
    token = msg.text.strip()

    if not re.match(r"^\d+:[A-Za-z0-9_-]{35,}$", token):
        admin_bot.send_message(
            msg.chat.id,
            "❌ Неверный формат токена.\n\n"
            "Пример: `1234567890:ABCdefGHI...`\n\n"
            "Попробуйте ещё раз или нажмите 🔙 Отмена",
            parse_mode="Markdown"
        )
        return   # остаёмся в состоянии awaiting_token для повторной попытки

    try:
        test = telebot.TeleBot(token)
        me   = test.get_me()

        create_bot(token, msg.chat.id)
        admin_user_state[msg.chat.id] = None

        admin_bot.send_message(
            msg.chat.id,
            f"✅ Бот *@{me.username}* успешно подключён!\n\n"
            f"🆔 ID: `{me.id}`\n"
            f"📡 Webhook активен\n\n"
            f"Перейдите в бота и напишите /start\n\n"
            f"💡 Совет: установите контакт менеджера командой /setcontact",
            parse_mode="Markdown",
            reply_markup=admin_main_menu()
        )
        logger.info(f"[SAAS] Подключён @{me.username} для owner={msg.chat.id}")

    except Exception as e:
        logger.error(f"[SAAS] Ошибка подключения: {e}")
        admin_bot.send_message(
            msg.chat.id,
            f"❌ Не удалось подключить бот.\n"
            f"Проверьте токен и попробуйте снова.\n\n"
            f"Ошибка: `{str(e)[:150]}`",
            parse_mode="Markdown"
        )
        # Остаёмся в awaiting_token — пользователь может попробовать ещё раз

# ── Catch-all admin-бота ─────────────────────────────────────
@admin_bot.message_handler(func=lambda m: True)
def admin_catch_all(msg):
    """
    [BTN_FIX] Ловит только то, что не обработано выше.
    Сюда НЕ попадают:
      - кнопки меню (обработаны своими хендлерами)
      - состояние awaiting_token (обработано выше)
    Поэтому больше нет ошибки «Неверный формат токена» на кнопки.
    """
    admin_user_state[msg.chat.id] = None
    admin_bot.send_message(
        msg.chat.id,
        "Используйте меню ниже 👇",
        reply_markup=admin_main_menu()
    )


# ═══════════════════════════════════════════════════════════════
# WEBHOOK ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/bot/<token>", methods=["POST"])
def webhook(token):
    """
    Webhook для клиентских ботов.
    Автовосстановление: если бот не в памяти — перезагружаем из файла.
    """
    if token not in bots:
        logger.warning(f"[WEBHOOK] Бот {token[:8]}... не найден, перезагружаем...")
        saved = load_bots()
        if token in saved:
            create_bot(token, saved[token])
        else:
            logger.error(f"[WEBHOOK] Бот {token[:8]}... не зарегистрирован")
            return "no bot", 404

    try:
        body   = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(body)
        bots[token].process_new_updates([update])
    except Exception as e:
        logger.error(f"[WEBHOOK] Ошибка обработки update: {e}")

    return "ok", 200

@app.route("/admin", methods=["POST"])
def admin_webhook():
    """Webhook для admin-бота (отдельный маршрут /admin)."""
    try:
        body   = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(body)
        admin_bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"[ADMIN WEBHOOK] Ошибка: {e}")
    return "ok", 200

@app.route("/health")
def health():
    """Health check для UptimeRobot / Render / мониторинга."""
    return {
        "status": "ok",
        "bots":   len(bots),
        "time":   datetime.utcnow().isoformat()
    }

@app.route("/")
def home():
    return "🚀 SaaS AI Sales Bot v2.1 — Production Ready", 200


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    logger.info("=" * 55)
    logger.info("  SaaS AI Sales Bot v2.1 — Запуск")
    logger.info("=" * 55)

    # Загружаем всех сохранённых клиентских ботов
    saved = load_bots()
    logger.info(f"Найдено ботов в реестре: {len(saved)}")
    for t, oid in saved.items():
        if t == ADMIN_TOKEN:
            # Admin-токен обрабатывается отдельно — не создаём дубликат
            logger.info("  Пропускаем admin-токен в реестре клиентских ботов")
            continue
        logger.info(f"  Загрузка бота {t[:8]}... owner={oid}")
        create_bot(t, oid)

    # Устанавливаем webhook для admin-бота
    try:
        admin_bot.remove_webhook()
        time.sleep(0.3)
        admin_bot.set_webhook(url=f"{BASE_URL}/admin")
        logger.info(f"[ADMIN] Webhook: {BASE_URL}/admin")
    except Exception as e:
        logger.error(f"[ADMIN] Ошибка webhook: {e}")

    port = int(os.getenv("PORT", 8080))
    logger.info(f"Flask запущен на порту {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
