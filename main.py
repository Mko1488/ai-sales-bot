from flask import Flask, request
import telebot

from bot.user_bot import create_user_bot
from bot.admin_bot import create_admin_bot
from config import ADMIN_TOKEN, BASE_URL

app = Flask(__name__)

bots = {}

def create_bot(token, owner):
    bot = create_user_bot(token)
    bots[token] = bot

    bot.remove_webhook()
    bot.set_webhook(url=f"{BASE_URL}/bot/{token}")

admin_bot = create_admin_bot(ADMIN_TOKEN, create_bot)

@app.route("/bot/<token>", methods=["POST"])
def webhook(token):
    if token in bots:
        update = telebot.types.Update.de_json(request.data.decode("utf-8"))
        bots[token].process_new_updates([update])
    return "ok"

@app.route("/admin", methods=["POST"])
def admin():
    update = telebot.types.Update.de_json(request.data.decode("utf-8"))
    admin_bot.process_new_updates([update])
    return "ok"

if __name__ == "__main__":
    admin_bot.remove_webhook()
    admin_bot.set_webhook(url=f"{BASE_URL}/admin")

    app.run(host="0.0.0.0", port=8080)
