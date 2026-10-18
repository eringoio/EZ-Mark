import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def check_health(url: str, timeout_seconds: float = 1.5) -> bool:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False

    return payload.get("status") == "ok" and payload.get("app") == "ezmark"


def wait_for_health(
    url: str,
    timeout_seconds: int = 30,
    interval_seconds: float = 0.5,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if check_health(url):
            return True
        time.sleep(interval_seconds)
    return check_health(url)
