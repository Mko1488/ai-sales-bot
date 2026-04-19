import json
import os
from services.sheets import save_to_sheet

DATA_FILE = "data/crm.json"

def load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

def save_lead(user_id, text):
    data = load()

    data[user_id] = {
        "last": text
    }

    save(data)

    save_to_sheet({
        "user_id": user_id,
        "text": text
    })
