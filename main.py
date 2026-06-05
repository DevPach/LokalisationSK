# -*- coding: utf-8 -*-

# Стандартные библиотеки
import os
import re
import logging
from typing import List, Dict

# Сторонние библиотеки
from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from pyproj import Transformer
from gigachat import GigaChat

# Библиотеки для Telegram-бота
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ==================== ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ====================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_VERIFY_SSL_CERTS = os.getenv("GIGACHAT_VERIFY_SSL_CERTS", "true").lower() == "true"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Не найден TELEGRAM_BOT_TOKEN в .env")
if not GIGACHAT_CREDENTIALS:
    raise RuntimeError("Не найден GIGACHAT_CREDENTIALS в .env")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==================== НАСТРОЙКИ ПОИСКА ====================
ALLOWED_REGIONS = [
    "Запорожская область",
    "Херсонская область",
    "Донецкая область",
    "Крым",
]

REGION_BOUNDS = {
    "Запорожская область": {"lat_min": 45.0, "lat_max": 48.6, "lon_min": 34.0, "lon_max": 38.0},
    "Херсонская область": {"lat_min": 45.0, "lat_max": 47.6, "lon_min": 31.0, "lon_max": 36.0},
    "Донецкая область": {"lat_min": 46.8, "lat_max": 49.3, "lon_min": 36.8, "lon_max": 40.3},
    "Крым": {"lat_min": 44.0, "lat_max": 46.4, "lon_min": 32.0, "lon_max": 36.8},
}

geolocator = Nominatim(user_agent="sk42_telegram_bot_educational_project")


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def clean_place_name_with_gigachat(user_text: str) -> str:
    """Извлекает название населённого пункта из сообщения пользователя через GigaChat."""
    prompt = f"""
Ты помощник для географического поиска.

Из текста пользователя извлеки только название населенного пункта.
Не добавляй область, страну, пояснения, кавычки и лишние слова.
Если пользователь ввел только название, верни его без изменений.

Текст пользователя:
{user_text}
"""
    try:
        with GigaChat(
            credentials=GIGACHAT_CREDENTIALS,
            verify_ssl_certs=GIGACHAT_VERIFY_SSL_CERTS,
        ) as client:
            response = client.chat(prompt)
            result = response.choices[0].message.content.strip()
            result = result.replace('"', "").replace("«", "").replace("»", "")
            return result
    except Exception as error:
        logger.warning("Ошибка GigaChat, используем исходный текст: %s", error)
        return user_text.strip()


def is_inside_region(lat: float, lon: float, region: str) -> bool:
    """Проверяет, находятся ли координаты внутри заданной области."""
    bounds = REGION_BOUNDS[region]
    return (
        bounds["lat_min"] <= lat <= bounds["lat_max"]
        and bounds["lon_min"] <= lon <= bounds["lon_max"]
    )


def detect_sk42_epsg_by_longitude(lon: float) -> int:
    """Определяет зону СК-42 по долготе (EPSG:28406 или 28407)."""
    if 30.0 <= lon < 36.0:
        return 28406
    if 36.0 <= lon <= 42.0:
        return 28407
    raise ValueError("Долгота вне поддерживаемых зон СК-42")


def wgs84_to_sk42(lat: float, lon: float) -> Dict[str, int]:
    """Преобразует WGS84 → СК-42, возвращает X, Y, EPSG."""
    epsg_code = detect_sk42_epsg_by_longitude(lon)
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg_code}", always_xy=True)
    easting, northing = transformer.transform(lon, lat)
    return {"x": round(northing), "y": round(easting), "epsg": epsg_code}


def normalize_name(name: str) -> str:
    """Приводит название к единому формату для сравнения."""
    name = name.lower().strip()
    name = name.replace("ё", "е")
    name = re.sub(r"\s+", " ", name)
    return name


def extract_display_name(raw_address: str, fallback: str) -> str:
    """Извлекает короткое название населённого пункта из адреса Nominatim."""
    if not raw_address:
        return fallback
    return raw_address.split(",")[0].strip()


def search_places(place_name: str) -> List[Dict]:
    """
    Ищет населённый пункт в разрешённых регионах.
    Возвращает список с координатами WGS84 и СК-42.
    """
    results = []
    seen = set()

    for region in ALLOWED_REGIONS:
        query = f"{place_name}, {region}"
        try:
            locations = geolocator.geocode(
                query, exactly_one=False, limit=10, addressdetails=True, language="ru"
            )
        except Exception as error:
            logger.warning("Ошибка геокодирования для %s: %s", query, error)
            continue

        if not locations:
            continue

        for location in locations:
            lat = float(location.latitude)
            lon = float(location.longitude)

            if not is_inside_region(lat, lon, region):
                continue

            display_name = extract_display_name(location.address, place_name)
            key = (normalize_name(display_name), region, round(lat, 5), round(lon, 5))
            if key in seen:
                continue
            seen.add(key)

            try:
                sk42 = wgs84_to_sk42(lat, lon)
            except ValueError:
                continue

            results.append({
                "region": region,
                "place": display_name,
                "x": sk42["x"],
                "y": sk42["y"],
                "epsg": sk42["epsg"],
                "lat": lat,
                "lon": lon,
            })

    return results


def format_results(results: List[Dict]) -> str:
    """
    Форматирует результаты поиска, добавляя для каждого варианта
    кликабельную ссылку на OpenStreetMap с маркером и зумом ~10,
    который показывает область ~40×40 км.
    """
    if not results:
        return (
            "Ничего не найдено в разрешенных регионах.\n\n"
            "Проверьте название населенного пункта или напишите его точнее."
        )

    lines = []
    for index, item in enumerate(results, start=1):
        lat = item["lat"]
        lon = item["lon"]

        # Ссылка на OpenStreetMap с маркером (mlat/mlon) и уровнем приближения 10
        map_url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}&zoom=10"

        lines.append(
            f"{index}. Область: {item['region']}\n"
            f"   Населенный пункт: {item['place']}\n"
            f"   Координаты: X-{item['x']}, Y-{item['y']}\n"
            f"   Система: СК-42, EPSG:{item['epsg']}\n"
            f"   [🗺️ Открыть на карте]({map_url})"
        )

    return "\n\n".join(lines)


# ==================== ОБРАБОТЧИКИ TELEGRAM ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Здравствуйте.\n\n"
        "Введите название населенного пункта, а я найду координаты в СК-42.\n\n"
        "Поиск ограничен:\n"
        "— Запорожская область\n"
        "— Херсонская область\n"
        "— Донецкая область\n"
        "— Крым\n\n"
        "Пример: Мелитополь"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Как пользоваться:\n\n"
        "1. Напишите название населенного пункта.\n"
        "2. Если найдено несколько совпадений, я выведу их под номерами.\n"
        "3. Формат ответа:\n"
        "   Область, населенный пункт, координаты X/Y в СК-42.\n"
        "4. Нажмите на ссылку **Открыть на карте** — откроется карта с маркером.\n\n"
        "Пример запроса:\n"
        "Бердянск"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text.strip()
    if not user_text:
        await update.message.reply_text("Введите название населенного пункта.")
        return

    await update.message.reply_text("Ищу населенный пункт и считаю координаты...")

    place_name = clean_place_name_with_gigachat(user_text)
    results = search_places(place_name)
    answer = format_results(results)

    # Отправляем текст с поддержкой Markdown (чтобы ссылка была кликабельной)
    await update.message.reply_text(answer, parse_mode="Markdown")


def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()