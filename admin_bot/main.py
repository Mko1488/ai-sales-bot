import logging
import os
from aiogram import Bot, Dispatcher, executor, types

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

users_bots = {}

def main_menu():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("➕ Добавить бота")
    keyboard.add("📂 Мои боты")
    return keyboard

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    await message.answer("Добро пожаловать 🤖", reply_markup=main_menu())

@dp.message_handler(lambda message: message.text == "➕ Добавить бота")
async def add_bot(message: types.Message):
    await message.answer("Отправь токен бота")

@dp.message_handler(lambda message: ":" in message.text)
async def save_bot(message: types.Message):
    user_id = message.from_user.id
    token = message.text.strip()

    if user_id not in users_bots:
        users_bots[user_id] = []

    users_bots[user_id].append(token)

    await message.answer("✅ Бот подключен!")

@dp.message_handler(lambda message: message.text == "📂 Мои боты")
async def my_bots(message: types.Message):
    user_id = message.from_user.id

    if user_id not in users_bots:
        await message.answer("Нет ботов")
        return

    text = "\n".join(users_bots[user_id])
    await message.answer(text)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
