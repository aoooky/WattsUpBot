import asyncio
import os
import re
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from openai import OpenAI

# ================== НАСТРОЙКИ ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENCHARGEMAP_KEY = os.getenv("OPENCHARGEMAP_KEY")  # API ключ Open Charge Map

if not BOT_TOKEN or not OPENAI_API_KEY or not OPENCHARGEMAP_KEY:
    raise ValueError("Не заданы BOT_TOKEN, OPENAI_API_KEY или OPENCHARGEMAP_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ================== ОГРАНИЧЕНИЕ ПО ТЕМАМ ==================

EV_KEYWORDS = [
    "электро", "электрокар", "электромобиль",
    "заряд", "батар", "квт", "км",
    "tesla", "nissan", "leaf", "model",
    "byd", "zeekr", "xiaomi", "ev",
    "запас хода", "cha", "ccs"
]

def is_ev_related(text: str) -> bool:
    text = text.lower()
    return any(word in text for word in EV_KEYWORDS)

# ================== СИСТЕМНЫЙ ПРОМПТ ==================

SYSTEM_PROMPT = """
Ты — специализированный помощник по электромобилям и поездкам.
Отвечай ТОЛЬКО на вопросы, связанные с электромобилями, батареями, запасом хода, маршрутами и зарядными станциями.
Если данных недостаточно, задавай уточняющие вопросы.
"""

# ================== ПАМЯТЬ ПОЛЬЗОВАТЕЛЕЙ ==================

user_contexts = {}   # user_id -> сообщения для OpenAI
user_data = {}       # user_id -> ключевые данные

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================

def extract_ev_data(text: str, data: dict) -> dict:
    text_lower = text.lower()

    # Модель авто
    model_match = re.search(r"(tesla|nissan|leaf|byd|zeekr|xiaomi|model\s?\w+)\s*[\w\d]*", text_lower)
    if model_match and not data.get("model"):
        data["model"] = model_match.group(0).title()

    # Уровень заряда
    charge_match = re.search(r"(\d{1,3})\s?%", text_lower)
    if charge_match and not data.get("charge"):
        data["charge"] = int(charge_match.group(1))

    # Старт
    start_match = re.search(r"(из|старт)\s*([\w\s\(\)-]+)", text_lower)
    if start_match and not data.get("start"):
        data["start"] = start_match.group(2).title()

    # Назначение
    dest_match = re.search(r"(в|до|destination)\s*([\w\s\(\)-]+)", text_lower)
    if dest_match and not data.get("destination"):
        data["destination"] = dest_match.group(2).title()

    # Маршрут / трасса
    if ("трасс" in text_lower or "route" in text_lower) and not data.get("route"):
        data["route"] = "по трассе"

    return data

def geocode_city(city_name):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": city_name, "format": "json", "limit": 1}
    response = requests.get(url, params=params, headers={"User-Agent": "WattsUpBot"})
    data = response.json()
    if data:
        return float(data[0]["lat"]), float(data[0]["lon"])
    return None, None

def find_charging_stations(lat, lon, radius_km=50):
    url = "https://api.openchargemap.io/v3/poi/"
    params = {
        "output": "json",
        "key": OPENCHARGEMAP_KEY,
        "latitude": lat,
        "longitude": lon,
        "distance": radius_km,
        "distanceunit": "KM",
        "maxresults": 10
    }
    response = requests.get(url, params=params)
    if response.status_code == 200:
        return response.json()
    return []

def format_stations(stations):
    if not stations:
        return "По данному участку маршрута зарядных станций не найдено."
    lines = []
    for s in stations:
        name = s.get("AddressInfo", {}).get("Title", "Без названия")
        addr = s.get("AddressInfo", {}).get("AddressLine1", "")
        connections = s.get("Connections", [])
        conn_types = ", ".join([c.get("ConnectionType", {}).get("Title", "?") for c in connections])
        lines.append(f"⚡ {name}\nАдрес: {addr}\nТипы разъёмов: {conn_types}")
    return "\n\n".join(lines)

# ================== ОБРАБОТЧИКИ ==================

@dp.message(CommandStart())
async def start(message: types.Message):
    user_id = message.from_user.id
    user_contexts[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    user_data[user_id] = {}

    await message.answer(
        "Привет! 🚗⚡\n"
        "Я помогаю планировать поездки на электромобилях.\n\n"
        "Напиши, например:\n"
        "«Tesla Model 3, еду из Минска в Москву»"
    )

@dp.message()
async def chat(message: types.Message):
    user_id = message.from_user.id
    text = message.text

    if not is_ev_related(text) and user_id not in user_data:
        await message.answer(
            "Я отвечаю только на вопросы по электромобилям.\n"
            "Например: модель авто, маршрут, зарядка."
        )
        return

    if user_id not in user_contexts:
        user_contexts[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if user_id not in user_data:
        user_data[user_id] = {}

    # Обновляем ключевые данные
    user_data[user_id] = extract_ev_data(text, user_data[user_id])

    # Формируем сообщение для OpenAI
    combined_message = text
    if user_data[user_id]:
        combined_message += "\n\nКлючевые данные пользователя:\n" + "\n".join(f"{k}: {v}" for k, v in user_data[user_id].items())
    user_contexts[user_id].append({"role": "user", "content": combined_message})

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=user_contexts[user_id]
        )
        answer = response.choices[0].message.content

        # Добавляем ответ бота
        user_contexts[user_id].append({"role": "assistant", "content": answer})
        await message.answer(answer)

        # ----------------- ДОБАВЛЯЕМ ЗАРЯДНЫЕ СТАНЦИИ -----------------
        if user_data[user_id].get("start") and user_data[user_id].get("destination"):
            start_lat, start_lon = geocode_city(user_data[user_id]["start"])
            end_lat, end_lon = geocode_city(user_data[user_id]["destination"])

            if start_lat and start_lon and end_lat and end_lon:
                stations_start = find_charging_stations(start_lat, start_lon)
                stations_end = find_charging_stations(end_lat, end_lon)

                stations_text = f"Зарядные станции на маршруте:\n\n"
                stations_text += f"В начале маршрута ({user_data[user_id]['start']}):\n{format_stations(stations_start)}\n\n"
                stations_text += f"В конце маршрута ({user_data[user_id]['destination']}):\n{format_stations(stations_end)}"

                await message.answer(stations_text)

        # Обрезка контекста
        if len(user_contexts[user_id]) > 30:
            user_contexts[user_id] = [user_contexts[user_id][0]] + user_contexts[user_id][-28:]

    except Exception as e:
        await message.answer("Произошла ошибка при расчёте. Попробуй ещё раз позже.")
        print("OpenAI error:", e)

# ================== ЗАПУСК ==================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
