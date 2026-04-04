"""
╔══════════════════════════════════════════════════════════╗
║         TELEGRAM SAAS AI SALES BOT — PRODUCTION         ║
║              Версия 2.0 | Полный апгрейд                 ║
╚══════════════════════════════════════════════════════════╝

УЛУЧШЕНИЯ:
  [AI]      — AI-продажник с этапами: потребность→бюджет→предложение→закрытие
  [CRM]     — Статусы cold/warm/hot/deal, история 10 сообщений, summary
  [SHEETS]  — Нормальная отправка с retry и обработкой ошибок
  [ADMIN]   — Полная информация по каждому лиду
  [SAAS]    — Полная изоляция данных между ботами
  [FOLLOW]  — Умный follow-up с учётом активности
  [STABLE]  — Logging, error handling, не падает
"""

import telebot
from telebot import types
from flask import Flask, request
import os, json, time, requests, re, threading, logging
from datetime import datetime

# ================= LOGGING [STABLE] =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("SaasBot")

# ================= ENV =================
ADMIN_TOKEN  = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "0"))
YANDEX_KEY   = os.getenv("YANDEX_API_KEY")
YANDEX_FID   = os.getenv("YANDEX_FOLDER_ID")
BASE_URL     = os.getenv("BASE_URL")
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "")

app = Flask(__name__)

# [SAAS] Глобальные хранилища — изолированы по токену
bots   = {}   # token -> TeleBot
owners = {}   # token -> owner_id

BOTS_FILE = "bots.json"

# ================= FILE STORAGE =================
def load_bots() -> dict:
    """Загружает список зарегистрированных ботов."""
    if os.path.exists(BOTS_FILE):
        try:
            with open(BOTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки {BOTS_FILE}: {e}")
    return {}

def save_bots(data: dict):
    """Сохраняет список ботов атомарно."""
    tmp = BOTS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BOTS_FILE)
    except Exception as e:
        logger.error(f"Ошибка сохранения ботов: {e}")

# ================= GOOGLE SHEETS [SHEETS] =================
def save_to_sheet(bot_token: str, user_id: str, name: str,
                  username: str, status: str, message: str,
                  summary: str = ""):
    """
    [SHEETS] Нормальная отправка с retry, обработкой ошибок и расширенными данными.
    Добавлено: bot_token для изоляции, summary клиента, timestamp.
    """
    if not GOOGLE_SHEET_URL:
        return
    payload = {
        "bot_token":  bot_token[:8],   # первые 8 символов — идентификатор бота
        "user_id":    user_id,
        "name":       name,
        "username":   username,
        "status":     status,
        "message":    message[:500],   # ограничиваем длину
        "summary":    summary[:300],
        "timestamp":  datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    }

    # [SHEETS] Retry 3 раза с паузой
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
                logger.warning(f"[SHEETS] HTTP {resp.status_code}, попытка {attempt+1}")
        except requests.exceptions.Timeout:
            logger.warning(f"[SHEETS] Timeout, попытка {attempt+1}")
        except Exception as e:
            logger.error(f"[SHEETS] Ошибка: {e}")
            return   # не-сетевая ошибка — не retry
        time.sleep(1.5 * (attempt + 1))

    logger.error(f"[SHEETS] Не удалось записать лид {user_id} после 3 попыток")

# ================= CREATE BOT =================
def create_bot(token: str, owner_id: int):
    """
    [SAAS] Создаёт полностью изолированный экземпляр бота.
    Каждый бот имеет свой файл БД, свою CRM, свою очередь follow-up.
    """
    if token in bots:
        logger.info(f"Бот {token[:8]}... уже запущен, пропускаем")
        return

    bot = telebot.TeleBot(token, threaded=False)
    logger.info(f"[SAAS] Инициализация бота {token[:8]}...")

    # [SAAS] Уникальный файл данных для каждого бота
    DATA_FILE = f"db_{token[:8]}.json"
    lock = threading.Lock()

    # ── Загрузка / сохранение данных ──────────────────────────
    def load_data() -> dict:
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки {DATA_FILE}: {e}")
        return {"products": {}, "crm": {}}

    def save_data():
        """[STABLE] Атомарная запись через tmp-файл, защита lock'ом."""
        tmp = DATA_FILE + ".tmp"
        with lock:
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"products": products, "crm": crm},
                              f, ensure_ascii=False, indent=2)
                os.replace(tmp, DATA_FILE)
            except Exception as e:
                logger.error(f"Ошибка сохранения {DATA_FILE}: {e}")

    raw      = load_data()
    products = raw.get("products", {})
    crm      = raw.get("crm", {})

    # [STABLE] In-memory состояния (не персистентные, это нормально)
    user_state   = {}   # chat_id -> state string
    follow_flags = {}   # chat_id -> bool (активен ли follow-up)
    last_active  = {}   # chat_id -> timestamp последней активности

    # ─────────────────────────────────────────────────────────
    def is_admin(msg) -> bool:
        return msg.chat.id == owner_id

    # ================= AI [AI] =================
    def ask_ai(chat_id: int, user_text: str):
        """
        [AI] Полноценный AI-продажник с:
          - системным промптом с этапами продаж
          - историей последних 10 сообщений
          - AI-определением статуса (не ключевые слова)
          - каталогом товаров в контексте
          - запретом агрессивных продаж
        """
        uid = str(chat_id)
        user_data = crm.get(uid, {})

        # [CRM] История последних 10 сообщений
        history = user_data.get("history", [])[-10:]
        summary = user_data.get("summary", "")

        # Каталог для контекста AI
        catalog_text = ""
        if products:
            lines = [f"  • {p['name']} — {p['price']}"
                     for p in products.values()]
            catalog_text = "КАТАЛОГ ТОВАРОВ:\n" + "\n".join(lines) + "\n\n"

        # Summary для контекста
        summary_text = f"КРАТКОЕ ДОСЬЕ НА КЛИЕНТА: {summary}\n\n" if summary else ""

        # [AI] Системный промпт с этапами продаж
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

        # Собираем messages для API
        messages = [{"role": "system", "text": system_prompt}]

        # [CRM] Добавляем историю диалога
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

            # [AI] Извлекаем статус из ответа AI
            status = "cold"
            m = re.search(r"\[STATUS:(HOT|WARM|COLD|DEAL)\]", raw_text, re.IGNORECASE)
            if m:
                status = m.group(1).lower()

            # Очищаем ответ от тега статуса
            clean = re.sub(r"\[STATUS:.*?\]", "", raw_text, flags=re.IGNORECASE).strip()

            logger.info(f"[AI] chat={chat_id} status={status} len={len(clean)}")
            return clean, status

        except requests.exceptions.Timeout:
            logger.warning(f"[AI] Timeout для chat_id={chat_id}")
            return "Можешь чуть подробнее описать, что тебя интересует?", "cold"
        except Exception as e:
            logger.error(f"[AI] Ошибка: {e}")
            return "Расскажи подробнее, чем могу помочь 👇", "cold"

    # ================= AI SUMMARY [CRM] =================
    def generate_summary(uid: str) -> str:
        """
        [CRM] Генерирует краткое AI-резюме клиента на основе истории.
        Вызывается после каждых 5 сообщений.
        """
        history = crm.get(uid, {}).get("history", [])
        if len(history) < 3:
            return crm.get(uid, {}).get("summary", "")

        dialog = "\n".join([
            f"{'Клиент' if h['role']=='user' else 'Менеджер'}: {h['text']}"
            for h in history[-12:]
        ])

        prompt = (
            "На основе диалога составь краткое досье на клиента (1-2 предложения).\n"
            "Укажи: интерес, потребность, готовность к покупке.\n"
            "Без лишних слов.\n\n"
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

    # ================= CRM [CRM] =================
    def save_lead(msg, status: str, ai_reply: str = None):
        """
        [CRM] Полноценное сохранение лида:
          - история сообщений (user + assistant)
          - статус определяется AI
          - summary обновляется каждые 5 сообщений
          - уведомление владельца при hot/deal
        """
        uid      = str(msg.chat.id)
        name     = (msg.from_user.first_name or "").strip()
        username = (msg.from_user.username or "").strip()

        # Инициализация если новый
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

        # Обновляем базовые поля
        crm[uid]["name"]       = name or crm[uid]["name"]
        crm[uid]["username"]   = username or crm[uid]["username"]
        crm[uid]["status"]     = status
        crm[uid]["last_msg"]   = msg.text
        crm[uid]["last_ts"]    = int(time.time())
        crm[uid]["msg_count"]  = crm[uid].get("msg_count", 0) + 1

        # [CRM] Добавляем сообщение в историю
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

        # [CRM] Ограничиваем историю 30 записями (15 диалогов)
        crm[uid]["history"] = crm[uid]["history"][-30:]

        # [CRM] Обновляем summary каждые 5 сообщений пользователя
        if crm[uid]["msg_count"] % 5 == 0:
            threading.Thread(
                target=lambda: _update_summary_async(uid),
                daemon=True
            ).start()

        save_data()

        # [SHEETS] Отправляем в Google Sheets асинхронно
        threading.Thread(
            target=save_to_sheet,
            args=(token, uid, name, username, status,
                  msg.text, crm[uid].get("summary", "")),
            daemon=True
        ).start()

        # [CRM] Уведомляем владельца при повышении статуса
        _notify_owner_if_needed(uid, name, username, msg.text,
                                status, prev_status)

    def _update_summary_async(uid: str):
        """[CRM] Асинхронное обновление summary."""
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

    def _notify_owner_if_needed(uid, name, username, text,
                                status, prev_status):
        """[CRM] Умные уведомления — только при повышении статуса."""
        status_order = {"cold": 0, "warm": 1, "hot": 2, "deal": 3}
        prev_rank = status_order.get(prev_status, 0)
        curr_rank = status_order.get(status, 0)

        if curr_rank <= prev_rank:
            return  # Статус не вырос — не спамим

        emoji = {"warm": "🌡", "hot": "🔥", "deal": "💰"}.get(status, "")
        if not emoji:
            return

        summary = crm.get(uid, {}).get("summary", "")
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
            bot.send_message(
                owner_id, text_notify,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"[CRM] Не удалось уведомить владельца: {e}")

    # ================= FOLLOW-UP [FOLLOW] =================
    def follow(chat_id: int):
        """
        [FOLLOW] Умный follow-up:
          - не стартует если уже активен
          - проверяет активность пользователя перед отправкой
          - 2 мягких касания с увеличивающимися интервалами
          - не спамит активным пользователям
        """
        if follow_flags.get(chat_id):
            return
        follow_flags[chat_id] = True
        last_active[chat_id] = time.time()

        def worker():
            # Первое касание — через 20 минут
            time.sleep(1200)
            if not follow_flags.get(chat_id):
                return
            # Проверяем: если пользователь написал в последние 15 минут — пропускаем
            if time.time() - last_active.get(chat_id, 0) < 900:
                pass  # Активен — сдвинем follow на потом, но не спамим
            else:
                try:
                    bot.send_message(
                        chat_id,
                        "Остались вопросы? Я тут 😊 Могу помочь с выбором"
                    )
                    logger.info(f"[FOLLOW] Follow-up 1 отправлен: {chat_id}")
                except Exception as e:
                    logger.warning(f"[FOLLOW] Ошибка follow-up 1: {e}")

            # Второе касание — ещё через 40 минут
            time.sleep(2400)
            if not follow_flags.get(chat_id):
                return
            if time.time() - last_active.get(chat_id, 0) < 1800:
                return  # Был активен — не спамим
            try:
                bot.send_message(
                    chat_id,
                    "Если что — пиши, помогу подобрать лучший вариант под твой запрос 🙌"
                )
                logger.info(f"[FOLLOW] Follow-up 2 отправлен: {chat_id}")
            except Exception as e:
                logger.warning(f"[FOLLOW] Ошибка follow-up 2: {e}")

            follow_flags[chat_id] = False  # Завершаем цикл

        threading.Thread(target=worker, daemon=True).start()

    def cancel_follow(chat_id: int):
        """[FOLLOW] Отмена follow-up при новом сообщении от пользователя."""
        follow_flags[chat_id] = False
        last_active[chat_id]  = time.time()

    # ================= UI =================
    def main_menu(msg):
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        kb.add("📦 Каталог", "💬 Написать менеджеру")
        if is_admin(msg):
            kb.row("📊 Лиды", "📢 Рассылка")
            kb.row("➕ Добавить товар", "📈 Статистика")
        return kb

    def admin_back_kb():
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("🔙 Назад")
        return kb

    # ================= HANDLERS =================

    @bot.message_handler(commands=["start"])
    def start(msg):
        name = msg.from_user.first_name or "друг"
        bot.send_message(
            msg.chat.id,
            f"Привет, {name}! 👋\n\nЧем могу помочь?",
            reply_markup=main_menu(msg)
        )
        logger.info(f"[BOT] /start от {msg.chat.id}")

    @bot.message_handler(func=lambda m: m.text == "📦 Каталог")
    def catalog(msg):
        if not products:
            bot.send_message(msg.chat.id, "Каталог пока пуст. Скоро добавим товары! 🔜")
            return
        lines = []
        for i, p in enumerate(products.values(), 1):
            lines.append(f"{i}. *{p['name']}* — {p['price']}")
            if p.get("desc"):
                lines.append(f"   _{p['desc']}_")
        bot.send_message(
            msg.chat.id,
            "📦 *Наш каталог:*\n\n" + "\n".join(lines),
            parse_mode="Markdown"
        )

    # ── ADMIN: Лиды [ADMIN] ──────────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "📊 Лиды")
    def leads(msg):
        if not is_admin(msg):
            return
        if not crm:
            bot.send_message(msg.chat.id, "Лидов пока нет 🤷")
            return

        # [ADMIN] Сортируем по статусу (горячие сначала)
        order = {"deal": 0, "hot": 1, "warm": 2, "cold": 3}
        sorted_leads = sorted(
            crm.items(),
            key=lambda x: (order.get(x[1].get("status", "cold"), 4),
                           -(x[1].get("last_ts", 0)))
        )

        emoji_map = {"hot": "🔥", "warm": "🌡", "cold": "❄️", "deal": "💰"}

        bot.send_message(
            msg.chat.id,
            f"📊 *Всего лидов: {len(crm)}*\n"
            f"💰 Сделок: {sum(1 for d in crm.values() if d.get('status')=='deal')} | "
            f"🔥 Горячих: {sum(1 for d in crm.values() if d.get('status')=='hot')} | "
            f"🌡 Тёплых: {sum(1 for d in crm.values() if d.get('status')=='warm')}",
            parse_mode="Markdown"
        )

        # [ADMIN] Полная информация по каждому лиду
        for uid, d in sorted_leads[:20]:   # лимит 20, чтобы не спамить
            status  = d.get("status", "cold")
            emoji   = emoji_map.get(status, "❓")
            name    = d.get("name", "—") or "—"
            uname   = f"@{d['username']}" if d.get("username") else "нет username"
            last    = d.get("last_msg", "—") or "—"
            summary = d.get("summary", "")
            count   = d.get("msg_count", 0)
            ts      = d.get("last_ts", 0)
            dt      = datetime.utcfromtimestamp(ts).strftime("%d.%m %H:%M") if ts else "—"

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
                f"_...и ещё {len(crm)-20} лидов. Для полного экспорта используй /export_",
                parse_mode="Markdown"
            )

    # ── ADMIN: Статистика [ADMIN] ────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "📈 Статистика")
    def stats(msg):
        if not is_admin(msg):
            return

        total  = len(crm)
        cold   = sum(1 for d in crm.values() if d.get("status") == "cold")
        warm   = sum(1 for d in crm.values() if d.get("status") == "warm")
        hot    = sum(1 for d in crm.values() if d.get("status") == "hot")
        deal   = sum(1 for d in crm.values() if d.get("status") == "deal")

        # Активные за последние 24ч
        day_ago = time.time() - 86400
        active_day = sum(
            1 for d in crm.values()
            if d.get("last_ts", 0) > day_ago
        )

        conv_rate = round(deal / total * 100, 1) if total else 0
        hot_rate  = round((hot + deal) / total * 100, 1) if total else 0

        text = (
            "📈 *СТАТИСТИКА CRM*\n\n"
            f"👥 Всего лидов: *{total}*\n"
            f"❄️ Cold: {cold}  🌡 Warm: {warm}  🔥 Hot: {hot}  💰 Deal: {deal}\n\n"
            f"🕐 Активных за 24ч: *{active_day}*\n"
            f"📊 Конверсия в сделку: *{conv_rate}%*\n"
            f"🎯 Hot rate: *{hot_rate}%*\n\n"
            f"📦 Товаров в каталоге: *{len(products)}*"
        )
        bot.send_message(msg.chat.id, text, parse_mode="Markdown")

    # ── ADMIN: Рассылка ──────────────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "📢 Рассылка")
    def broadcast_start(msg):
        if not is_admin(msg):
            return
        user_state[msg.chat.id] = "broadcast"
        bot.send_message(
            msg.chat.id,
            "Введите текст рассылки.\n"
            "⚠️ Будет отправлен всем пользователям из CRM.\n"
            "Для отмены нажмите /cancel",
            reply_markup=admin_back_kb()
        )

    @bot.message_handler(commands=["cancel"])
    def cancel_cmd(msg):
        user_state[msg.chat.id] = None
        bot.send_message(msg.chat.id, "Отменено.", reply_markup=main_menu(msg))

    @bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "broadcast")
    def broadcast_send(msg):
        if not is_admin(msg):
            return
        if msg.text == "🔙 Назад":
            user_state[msg.chat.id] = None
            bot.send_message(msg.chat.id, "Отменено.", reply_markup=main_menu(msg))
            return

        sent = failed = 0
        for uid in list(crm.keys()):
            try:
                bot.send_message(int(uid), msg.text)
                sent += 1
                time.sleep(0.05)  # защита от флуда
            except Exception:
                failed += 1

        user_state[msg.chat.id] = None
        bot.send_message(
            msg.chat.id,
            f"✅ Рассылка завершена\n✔️ Доставлено: {sent}\n❌ Ошибок: {failed}",
            reply_markup=main_menu(msg)
        )
        logger.info(f"[ADMIN] Рассылка: sent={sent}, failed={failed}")

    # ── ADMIN: Добавить товар ────────────────────────────────
    @bot.message_handler(func=lambda m: m.text == "➕ Добавить товар")
    def add_product_start(msg):
        if not is_admin(msg):
            return
        user_state[msg.chat.id] = "add_product_name"
        bot.send_message(
            msg.chat.id,
            "Введите название товара:",
            reply_markup=admin_back_kb()
        )

    @bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "add_product_name")
    def add_product_name(msg):
        if not is_admin(msg):
            return
        if msg.text == "🔙 Назад":
            user_state[msg.chat.id] = None
            bot.send_message(msg.chat.id, "Отменено.", reply_markup=main_menu(msg))
            return
        user_state[msg.chat.id] = f"add_product_price|{msg.text}"
        bot.send_message(msg.chat.id, f"Товар: *{msg.text}*\n\nВведите цену:",
                         parse_mode="Markdown")

    @bot.message_handler(func=lambda m: str(user_state.get(m.chat.id, "")).startswith("add_product_price|"))
    def add_product_price(msg):
        if not is_admin(msg):
            return
        if msg.text == "🔙 Назад":
            user_state[msg.chat.id] = None
            bot.send_message(msg.chat.id, "Отменено.", reply_markup=main_menu(msg))
            return
        state = user_state[msg.chat.id]
        product_name = state.split("|", 1)[1]
        price = msg.text.strip()

        pid = str(int(time.time()))
        products[pid] = {"name": product_name, "price": price, "desc": ""}
        save_data()

        user_state[msg.chat.id] = None
        bot.send_message(
            msg.chat.id,
            f"✅ Товар добавлен:\n*{product_name}* — {price}",
            parse_mode="Markdown",
            reply_markup=main_menu(msg)
        )
        logger.info(f"[ADMIN] Добавлен товар: {product_name}")

    # ── ADMIN: Экспорт ───────────────────────────────────────
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

            csv_data = "\n".join(lines).encode("utf-8-sig")
            fname = f"crm_export_{token[:6]}_{int(time.time())}.csv"

            import io
            bot.send_document(
                msg.chat.id,
                (fname, io.BytesIO(csv_data)),
                caption=f"📊 Экспорт CRM: {len(crm)} лидов"
            )
        except Exception as e:
            logger.error(f"[ADMIN] Export error: {e}")
            bot.send_message(msg.chat.id, f"❌ Ошибка экспорта: {e}")

    # ── AI Handler [AI] ──────────────────────────────────────
    @bot.message_handler(func=lambda m: True)
    def handle_all(msg):
        """
        [AI] Главный обработчик:
          - отменяет follow-up при новом сообщении
          - вызывает AI с полной историей
          - сохраняет историю диалога
          - запускает умный follow-up
        """
        # [FOLLOW] Отменяем follow-up — пользователь активен
        cancel_follow(msg.chat.id)

        # Пропускаем служебные состояния (рассылка и т.д.)
        state = user_state.get(msg.chat.id)
        if state and state not in ["None", None]:
            return

        # Проверяем что текст не пуст
        if not msg.text or not msg.text.strip():
            return

        # [AI] Получаем ответ AI с историей
        answer, status = ask_ai(msg.chat.id, msg.text)

        # Отправляем ответ
        try:
            bot.send_message(msg.chat.id, answer)
        except Exception as e:
            logger.error(f"[BOT] Ошибка отправки: {e}")
            return

        # [CRM] Сохраняем лид с историей
        save_lead(msg, status, answer)

        # [FOLLOW] Запускаем умный follow-up
        follow(msg.chat.id)

    # ── Регистрация бота ─────────────────────────────────────
    bots[token]  = bot
    owners[token] = owner_id

    # [SAAS] Сохраняем в реестр
    data = load_bots()
    data[token] = owner_id
    save_bots(data)

    # Устанавливаем webhook
    try:
        bot.remove_webhook()
        time.sleep(0.3)
        bot.set_webhook(url=f"{BASE_URL}/bot/{token}")
        logger.info(f"[SAAS] Webhook установлен: {BASE_URL}/bot/{token[:8]}...")
    except Exception as e:
        logger.error(f"[SAAS] Ошибка webhook: {e}")

# ================= ADMIN BOT =================
admin_bot = telebot.TeleBot(ADMIN_TOKEN, threaded=False)

@admin_bot.message_handler(commands=["start"])
def admin_start(msg):
    admin_bot.send_message(
        msg.chat.id,
        "🚀 *AI Sales Bot — Панель управления*\n\n"
        "Отправь токен своего бота чтобы подключить его.\n\n"
        "Получить токен: @BotFather → /newbot",
        parse_mode="Markdown"
    )

@admin_bot.message_handler(func=lambda m: True)
def connect_bot(msg):
    """[SAAS] Подключение нового бота с валидацией токена."""
    token = msg.text.strip()

    # Базовая валидация формата токена
    if not re.match(r"^\d+:[A-Za-z0-9_-]{35,}$", token):
        admin_bot.send_message(
            msg.chat.id,
            "❌ Неверный формат токена.\n"
            "Пример: `1234567890:ABCdef...`",
            parse_mode="Markdown"
        )
        return

    try:
        test = telebot.TeleBot(token)
        me   = test.get_me()

        create_bot(token, msg.chat.id)

        admin_bot.send_message(
            msg.chat.id,
            f"✅ Бот *@{me.username}* успешно подключён!\n\n"
            f"🆔 ID: `{me.id}`\n"
            f"📡 Webhook активен\n\n"
            f"Теперь перейди к боту и попробуй написать что-нибудь.",
            parse_mode="Markdown"
        )
        logger.info(f"[SAAS] Подключён бот @{me.username} для owner={msg.chat.id}")

    except Exception as e:
        logger.error(f"[SAAS] Ошибка подключения: {e}")
        admin_bot.send_message(
            msg.chat.id,
            f"❌ Не удалось подключить бот.\n"
            f"Проверь токен и попробуй снова.\n\n"
            f"Ошибка: `{str(e)[:100]}`",
            parse_mode="Markdown"
        )

# ================= WEBHOOK ROUTES =================
@app.route("/bot/<token>", methods=["POST"])
def webhook(token):
    """
    [STABLE] Webhook с автовосстановлением бота.
    Если бот не найден в памяти — перезагружаем из файла.
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
        body = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(body)
        bots[token].process_new_updates([update])
    except Exception as e:
        logger.error(f"[WEBHOOK] Ошибка обработки update: {e}")

    return "ok", 200

@app.route("/admin", methods=["POST"])
def admin_webhook():
    """[STABLE] Webhook для admin-бота."""
    try:
        body = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(body)
        admin_bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"[ADMIN WEBHOOK] Ошибка: {e}")
    return "ok", 200

@app.route("/health")
def health():
    """[STABLE] Health check endpoint для мониторинга."""
    return {
        "status": "ok",
        "bots":   len(bots),
        "time":   datetime.utcnow().isoformat()
    }

@app.route("/")
def home():
    return "🚀 SaaS AI Sales Bot — Production Ready", 200

# ================= START =================
if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("  SaaS AI Sales Bot v2.0 — Запуск")
    logger.info("=" * 50)

    # [SAAS] Перезагружаем всех ботов из реестра
    saved = load_bots()
    logger.info(f"Найдено ботов в реестре: {len(saved)}")
    for t, oid in saved.items():
        logger.info(f"  Загрузка бота {t[:8]}... owner={oid}")
        create_bot(t, oid)

    # Admin-бот тоже регистрируем через webhook
    try:
        admin_bot.remove_webhook()
        time.sleep(0.3)
        admin_bot.set_webhook(url=f"{BASE_URL}/admin")
        logger.info(f"[ADMIN] Webhook установлен: {BASE_URL}/admin")
    except Exception as e:
        logger.error(f"[ADMIN] Ошибка webhook: {e}")

    port = int(os.getenv("PORT", 8080))
    logger.info(f"Flask запущен на порту {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
