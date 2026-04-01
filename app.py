import telebot
from telebot import types
from flask import Flask, request
import os

TOKEN = os.environ.get("ADMIN_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID"))

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

products = {}
user_state = {}

# ===== СТАРТ === ==
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "🔥 Напиши 'каталог' чтобы посмотреть товары")

# ===== КАТАЛОГ =====
@bot.message_handler(func=lambda m: m.text and "каталог" in m.text.lower())
def catalog(message):
    if not products:
        bot.send_message(message.chat.id, "❌ Пока нет товаров")
        return

    text = "📦 Товары:\n\n"
    for item in products.values():
        text += f"{item['name']} — {item['price']}$ (осталось {item['stock']})\n"

    bot.send_message(message.chat.id, text)

# ===== АДМИН =====
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID:
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Добавить товар", "❌ Удалить товар", "📊 Список товаров")

    bot.send_message(message.chat.id, "⚙️ Админ панель", reply_markup=markup)

# ===== ДОБАВИТЬ =====
@bot.message_handler(func=lambda m: m.text == "➕ Добавить товар")
def add_product(message):
    user_state[message.chat.id] = {"step": "name"}
    bot.send_message(message.chat.id, "Название товара:")

@bot.message_handler(func=lambda m: step(m, "name"))
def get_name(message):
    user_state[message.chat.id]["name"] = message.text
    user_state[message.chat.id]["step"] = "price"
    bot.send_message(message.chat.id, "Цена:")

@bot.message_handler(func=lambda m: step(m, "price"))
def get_price(message):
    user_state[message.chat.id]["price"] = int(message.text)
    user_state[message.chat.id]["step"] = "stock"
    bot.send_message(message.chat.id, "Количество:")

@bot.message_handler(func=lambda m: step(m, "stock"))
def get_stock(message):
    data = user_state[message.chat.id]
    key = data["name"].lower()

    products[key] = {
        "name": data["name"],
        "price": data["price"],
        "stock": int(message.text)
    }

    user_state.pop(message.chat.id)
    bot.send_message(message.chat.id, "✅ Товар добавлен")

# ===== УДАЛИТЬ =====
@bot.message_handler(func=lambda m: m.text == "❌ Удалить товар")
def delete_product(message):
    user_state[message.chat.id] = {"step": "delete"}
    bot.send_message(message.chat.id, "Название товара:")

@bot.message_handler(func=lambda m: step(m, "delete"))
def confirm_delete(message):
    key = message.text.lower()

    if key in products:
        products.pop(key)
        bot.send_message(message.chat.id, "🗑 Удалено")
    else:
        bot.send_message(message.chat.id, "❌ Не найдено")

    user_state.pop(message.chat.id)

# ===== СПИСОК =====
@bot.message_handler(func=lambda m: m.text == "📊 Список товаров")
def list_products(message):
    if not products:
        bot.send_message(message.chat.id, "Пусто")
        return

    text = ""
    for item in products.values():
        text += f"{item['name']} — {item['price']}$ ({item['stock']})\n"

    bot.send_message(message.chat.id, text)

# ===== ПОКУПКА =====
@bot.message_handler(func=lambda m: True)
def buy(message):
    text = message.text.lower()

    for key, item in products.items():
        if key in text:
            if item["stock"] > 0:
                item["stock"] -= 1
                bot.send_message(message.chat.id, f"✅ {item['name']} куплен")
                return
            else:
                bot.send_message(message.chat.id, "❌ Нет в наличии")
                return

# ===== UTILS =====
def step(message, s):
    return message.chat.id in user_state and user_state[message.chat.id]["step"] == s

# ===== WEBHOOK =====
@app.route('/admin', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('UTF-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return 'ok', 200

@app.route('/')
def index():
    return 'OK'

# ===== ЗАПУСК =====
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=os.environ.get("BASE_URL") + "/admin")

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
