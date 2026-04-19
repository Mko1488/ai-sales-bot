import requests
from config import YANDEX_KEY, YANDEX_FOLDER

def ask_ai(text):
    try:
        url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

        resp = requests.post(
            url,
            headers={"Authorization": f"Api-Key {YANDEX_KEY}"},
            json={
                "modelUri": f"gpt://{YANDEX_FOLDER}/yandexgpt-lite",
                "completionOptions": {"temperature": 0.6, "maxTokens": 200},
                "messages": [
                    {"role": "system", "text": "Ты продавец. Коротко веди к покупке."},
                    {"role": "user", "text": text}
                ]
            }
        )

        return resp.json()["result"]["alternatives"][0]["message"]["text"]

    except:
        return "Расскажи подробнее 👇"
