import logging
import os
from aiogram import Bot, Dispatcher, executor, types

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("CLIENT_BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

def menu():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("📦 Каталог")
    keyboard.add("💬 Менеджер")
    return keyboard

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    await message.answer("Добро пожаловать в магазин 🛍", reply_markup=menu())

@dp.message_handler(lambda message: message.text == "📦 Каталог")
async def catalog(message: types.Message):
    await message.answer("Вот каталог")

@dp.message_handler(lambda message: message.text == "💬 Менеджер")
async def manager(message: types.Message):
    await message.answer("Напишите ваш вопрос")

@dp.message_handler()
async def all_msg(message: types.Message):
    await message.answer("Менеджер скоро ответит")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
