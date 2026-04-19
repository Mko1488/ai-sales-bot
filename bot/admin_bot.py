import telebot

def create_admin_bot(token, create_bot_func):

    bot = telebot.TeleBot(token)

    @bot.message_handler(commands=["start"])
    def start(msg):
        bot.send_message(msg.chat.id, "Пришли токен бота")

    @bot.message_handler(func=lambda m: True)
    def connect(msg):
        token = msg.text.strip()

        try:
            create_bot_func(token, msg.chat.id)
            bot.send_message(msg.chat.id, "Бот подключен ✅")
        except:
            bot.send_message(msg.chat.id, "Ошибка ❌")

    return bot
