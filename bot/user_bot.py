import telebot
from services.ai import ask_ai
from services.crm import save_lead

def create_user_bot(token):

    bot = telebot.TeleBot(token)

    @bot.message_handler(commands=["start"])
    def start(msg):
        bot.send_message(msg.chat.id, "Привет 👋")

    @bot.message_handler(func=lambda m: True)
    def handle(msg):
        answer = ask_ai(msg.text)

        bot.send_message(msg.chat.id, answer)

        save_lead(str(msg.chat.id), msg.text)

    return bot
