"""Current weather and a short forecast from Open-Meteo (free, no key, reachable from here)."""
from __future__ import annotations

import json
from urllib.parse import quote_plus

from app.browsing.fetch import fetch

CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def _json(url: str) -> dict:
    return json.loads(fetch(url).body)


def report(location: str) -> str:
    place = location.split(",")[0].strip()
    found = _json(
        f"https://geocoding-api.open-meteo.com/v1/search?name={quote_plus(place)}&count=5&language=en&format=json"
    ).get("results") or []
    if not found:
        return f"ERROR: no place called '{location}' was found"
    # Prefer a match in the country or region the person named, e.g. "Hyderabad, India".
    hint = location.split(",", 1)[1].strip().lower() if "," in location else ""
    spot = next((p for p in found if hint and hint in f"{p.get('country', '')} {p.get('admin1', '')}".lower()), found[0])
    data = _json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={spot['latitude']}&longitude={spot['longitude']}&timezone=auto&forecast_days=3"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
    )
    cur, daily = data.get("current", {}), data.get("daily", {})
    name = ", ".join(x for x in (spot.get("name"), spot.get("admin1"), spot.get("country")) if x)
    lines = [
        f"Weather for {name} (Open-Meteo), local time {cur.get('time', '?')}:",
        f"Now: {CODES.get(cur.get('weather_code'), 'unknown')}, {cur.get('temperature_2m')}°C "
        f"(feels like {cur.get('apparent_temperature')}°C), humidity {cur.get('relative_humidity_2m')}%, "
        f"wind {cur.get('wind_speed_10m')} km/h, precipitation {cur.get('precipitation')} mm",
    ]
    for i, day in enumerate(daily.get("time", [])):
        lines.append(
            f"{day}: {CODES.get(daily['weather_code'][i], 'unknown')}, {daily['temperature_2m_min'][i]}–"
            f"{daily['temperature_2m_max'][i]}°C, chance of rain {daily['precipitation_probability_max'][i]}%"
        )
    return "\n".join(lines)
