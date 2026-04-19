import requests
from config import GOOGLE_SHEET_URL

def save_to_sheet(data):
    if not GOOGLE_SHEET_URL:
        print("Нет GOOGLE_SHEET_URL")
        return

    try:
        requests.post(
            GOOGLE_SHEET_URL,
            json=data,
            timeout=5
        )
        print("Отправлено в таблицу")

    except Exception as e:
        print("Ошибка таблицы:", e)
