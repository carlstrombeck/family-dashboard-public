"""Weather from Open-Meteo (free, no API key): now, today and tomorrow for one place."""

from __future__ import annotations

import requests

API = "https://api.open-meteo.com/v1/forecast"


def fetch_weather(lat: float, lon: float, place: str) -> dict:
    resp = requests.get(
        API,
        params={
            "latitude": lat,
            "longitude": lon,
            "timezone": "auto",
            "forecast_days": 2,
            "wind_speed_unit": "ms",
            "current": "temperature_2m,weather_code,is_day",
            "hourly": "temperature_2m,weather_code,precipitation_probability,is_day",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "precipitation_sum,wind_speed_10m_max",
        },
        timeout=10,
    )
    resp.raise_for_status()
    return summarize(resp.json(), place)


def summarize(data: dict, place: str) -> dict:
    """Trim Open-Meteo's column-oriented response to what the dashboard shows."""
    current = data.get("current", {})
    daily = data.get("daily", {})
    hourly = data.get("hourly", {})

    def column(block: dict, key: str, i: int):
        values = block.get(key) or []
        return values[i] if i < len(values) else None

    days = [
        {
            "date": date,
            "code": column(daily, "weather_code", i),
            "max": column(daily, "temperature_2m_max", i),
            "min": column(daily, "temperature_2m_min", i),
            "rainChance": column(daily, "precipitation_probability_max", i),
            "rain": column(daily, "precipitation_sum", i),
            "wind": column(daily, "wind_speed_10m_max", i),
        }
        for i, date in enumerate(daily.get("time") or [])
    ]
    hours = [
        {
            "time": time,  # local time at the place, e.g. "2026-09-25T16:00"
            "temp": column(hourly, "temperature_2m", i),
            "code": column(hourly, "weather_code", i),
            "rainChance": column(hourly, "precipitation_probability", i),
            "isDay": bool(column(hourly, "is_day", i)),
        }
        for i, time in enumerate(hourly.get("time") or [])
    ]
    return {
        "place": place,
        "now": {
            "temp": current.get("temperature_2m"),
            "code": current.get("weather_code"),
            "isDay": bool(current.get("is_day", 1)),
        },
        "days": days,
        "hours": hours,
    }
