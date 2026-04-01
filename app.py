import telebot
from telebot import types

TOKEN = "8597750213:AAF33ulRuuLjtFruKNKAn8MocGnaOUMRzK0"
ADMIN_ID = 700114731

bot = telebot.TeleBot(TOKEN)

# ===== ХРАНЕНИЕ =====
products = {}
user_state = {}

# ===== СТАРТ =====
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
    for key, item in products.items():
        text += f"{item['name']} — {item['price']}$ (осталось {item['stock']})\n"

    bot.send_message(message.chat.id, text)

# ===== АДМИН ПАНЕЛЬ = = = = =
@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.chat.id != ADMIN_ID:
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Добавить товар")
    markup.add("✏️ Изменить товар")
    markup.add("❌ Удалить товар")
    markup.add("📊 Список товаров")

    bot.send_message(message.chat.id, "⚙️ Админ панель", reply_markup=markup)

# ===== ДОБАВЛЕНИЕ =====
@bot.message_handler(func=lambda m: m.text == "➕ Добавить товар")
def add_product(message):
    bot.send_message(message.chat.id, "Введи название товара:")
    user_state[message.chat.id] = {"step": "name"}

@bot.message_handler(func=lambda m: message_step(m, "name"))
def get_name(message):
    user_state[message.chat.id]["name"] = message.text
    user_state[message.chat.id]["step"] = "price"
    bot.send_message(message.chat.id, "Введи цену:")

@bot.message_handler(func=lambda m: message_step(m, "price"))
def get_price(message):
    user_state[message.chat.id]["price"] = int(message.text)
    user_state[message.chat.id]["step"] = "stock"
    bot.send_message(message.chat.id, "Введи количество:")

@bot.message_handler(func=lambda m: message_step(m, "stock"))
def get_stock(message):
    data = user_state[message.chat.id]
    key = data["name"].lower()

    products[key] = {
        "name": data["name"],
        "price": data["price"],
        "stock": int(message.text)
    }

    bot.send_message(message.chat.id, "✅ Товар добавлен")
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

# ===== УДАЛЕНИЕ =====
@bot.message_handler(func=lambda m: m.text == "❌ Удалить товар")
def delete_product(message):
    bot.send_message(message.chat.id, "Напиши название товара для удаления:")
    user_state[message.chat.id] = {"step": "delete"}

@bot.message_handler(func=lambda m: message_step(m, "delete"))
def confirm_delete(message):
    key = message.text.lower()
    if key in products:
        products.pop(key)
        bot.send_message(message.chat.id, "🗑 Удалено")
    else:
        bot.send_message(message.chat.id, "❌ Не найдено")

    user_state.pop(message.chat.id)

# ===== ПОКУПКА =====
@bot.message_handler(func=lambda m: True)
def buy(message):
    text = message.text.lower()

    for key, item in products.items():
        if key in text:
            if item["stock"] > 0:
                item["stock"] -= 1
                bot.send_message(message.chat.id, f"✅ {item['name']}\nЦена: {item['price']}$\nНапиши 'оформить'")
                return
            else:
                bot.send_message(message.chat.id, "❌ Нет в наличии")
                return

# ===== УТИЛИТА =====
def message_step(message, step):
    return message.chat.id in user_state and user_state[message.chat.id]["step"] == step
    



