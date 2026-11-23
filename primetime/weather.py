import json
import threading
import time
import urllib.parse
import urllib.request

# ----- Location (TODO: set to your real location) -----
LOCATION_NAME = "College Station, TX"   # shown on the weather card
LAT = 30.62798
LON = -96.33441

API_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_SECONDS = 15 * 60      # refresh at most every 15 minutes
RETRY_SECONDS = 60           # after a failed fetch, wait this long before trying again
TIMEOUT_SECONDS = 5

# Cache: the last good icon name and when we last tried to fetch
_cached_bucket = None
_last_attempt = 0.0
_lock = threading.Lock()     # Flask can handle requests in parallel


def weather_bucket(weather_code, is_day):
    """Map an Open-Meteo WMO weather code to an icon name for the weather card.

    is_day: 1 = day, 0 = night (anything other than 0 counts as day).
    """
    day = is_day != 0

    if weather_code == 0:
        return "clear-day" if day else "clear-night"
    elif weather_code <= 3:
        return "partly-cloudy-day" if day else "partly-cloudy-night"
    elif weather_code <= 48:
        return "fog"
    elif weather_code <= 67 or 80 <= weather_code <= 82:
        return "rain"
    elif weather_code <= 86:
        return "snow"
    elif weather_code <= 99:
        return "thunder"
    else:
        return "unknown"    # unexpected code: card still renders


def _fetch_current():
    """Ask Open-Meteo for the current weather code and day/night flag."""
    query = urllib.parse.urlencode({
        "latitude": LAT,
        "longitude": LON,
        "current": "weather_code,is_day",
    })
    with urllib.request.urlopen(f"{API_URL}?{query}", timeout=TIMEOUT_SECONDS) as resp:
        data = json.load(resp)
    current = data["current"]
    return current["weather_code"], current["is_day"]


def get_weather_bucket():
    """Icon name for the weather card, cached so the API is hit at most every 15 min.

    Never raises: on failure it returns the last good value, or "unknown".
    """
    global _cached_bucket, _last_attempt

    with _lock:
        now = time.time()
        wait = CACHE_SECONDS if _cached_bucket is not None else RETRY_SECONDS
        if now - _last_attempt < wait:
            return _cached_bucket or "unknown"

        _last_attempt = now
        try:
            code, is_day = _fetch_current()
            _cached_bucket = weather_bucket(code, is_day)
        except Exception as e:   # network down, bad JSON, etc.
            print(f"[weather] fetch failed: {e}")
            # keep the old _cached_bucket; retry after RETRY_SECONDS if we have none

        return _cached_bucket or "unknown"


if __name__ == "__main__":
    tests = [
        (0, 1, "clear-day"),
        (0, 0, "clear-night"),
        (2, 1, "partly-cloudy-day"),
        (3, 0, "partly-cloudy-night"),
        (45, 1, "fog"),
        (61, 1, "rain"),
        (81, 0, "rain"),
        (73, 1, "snow"),
        (85, 1, "snow"),
        (95, 1, "thunder"),
        (123, 1, "unknown"),
    ]
    for code, is_day, expected in tests:
        result = weather_bucket(code, is_day)
        mark = "ok" if result == expected else "FAIL"
        print(f"{mark:4} code={code:3} is_day={is_day} -> {result}")

    print()
    print(f"Live ({LOCATION_NAME}):", get_weather_bucket())
    print("Second call (from cache):", get_weather_bucket())
