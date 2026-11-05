"""Bounded weather reads with a server-only, expiring Key Vault credential."""

import json
import math
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

WEATHER_TTL = 600
SECRET_TTL = 300
FAILURE_TTL = 60
DAILY_ATTEMPTS = 500
MAX_RESPONSE_BYTES = 64 * 1024


class DependencyError(Exception):
    def __init__(self, code, message, status=503):
        super().__init__(message)
        self.code = code
        self.status = status


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_weather(key, coordinates):
    url = "https://api.weatherapi.com/v1/current.json?" + urlencode(
        {"key": key, "q": coordinates, "aqi": "no"}
    )
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with build_opener(NoRedirects()).open(request, timeout=8) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        code = "provider_auth" if error.code in (401, 403) else "provider_unavailable"
        error.close()
        raise DependencyError(code, "Weather provider rejected the request.") from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise DependencyError("provider_unavailable", "Weather provider is unavailable.") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise DependencyError("provider_response", "Weather response exceeded the size limit.")
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise DependencyError("provider_response", "Weather provider returned invalid JSON.") from None


def curated_weather(payload):
    try:
        current = payload["current"]
        values = {
            "temperature": current["temp_c"],
            "feelsLike": current["feelslike_c"],
            "wind": current["wind_kph"],
            "humidity": current["humidity"],
            "observedAt": current["last_updated_epoch"],
        }
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values.values()
        ):
            raise ValueError
        if not (0 <= values["humidity"] <= 100 and values["wind"] >= 0 and values["observedAt"] > 0):
            raise ValueError
        condition = current["condition"]["text"]
        if not isinstance(condition, str) or not 1 <= len(condition) <= 100:
            raise ValueError
        return {**values, "condition": condition}
    except (KeyError, TypeError, ValueError):
        raise DependencyError("provider_response", "Weather provider returned unexpected data.") from None


class WeatherService:
    def __init__(self, load_secret, fetch=fetch_weather, clock=time.time, expires_at=None):
        self.load_secret = load_secret
        self.fetch = fetch
        self.clock = clock
        self.expires_at = expires_at
        self.lock = threading.Lock()
        self.secret = None
        self.secret_until = 0
        self.secret_failure = None
        self.secret_retry_at = 0
        self.weather = {}
        self.failures = {}
        self.day = None
        self.attempts = 0

    def _check_deadline(self, now):
        if self.expires_at is not None and now >= self.expires_at:
            self.secret = None
            self.secret_until = 0
            raise DependencyError("demo_expired", "The sample's test window has ended. Its owner must approve a new window.", 410)

    def _key(self, now):
        if self.secret and now < self.secret_until:
            return self.secret
        if self.secret_failure and now < self.secret_retry_at:
            raise self.secret_failure
        self.secret = None
        try:
            secret = self.load_secret()
            if secret.properties.enabled is False:
                raise DependencyError("secret_disabled", "The configured Key Vault secret is disabled.")
            expires = secret.properties.expires_on
            expiry = expires.timestamp() if expires else now + SECRET_TTL
            if expiry <= now:
                raise DependencyError("secret_expired", "The configured Key Vault secret has expired.")
            if not isinstance(secret.value, str) or not secret.value or len(secret.value) > 1024:
                raise DependencyError("secret_invalid", "The configured Key Vault secret is invalid.")
            self.secret = secret.value
            self.secret_until = min(now + SECRET_TTL, expiry)
            self.secret_failure = None
            return self.secret
        except DependencyError as error:
            self.secret_failure = error
            self.secret_retry_at = now + FAILURE_TTL
            raise

    def read(self, location):
        with self.lock:
            now = self.clock()
            self._check_deadline(now)
            cached = self.weather.get(location["id"])
            if cached and now < cached["cacheExpiresAt"]:
                return {**cached, "cached": True}
            failure = self.failures.get(location["id"])
            if failure and now < failure[0]:
                raise failure[1]
            try:
                day = datetime.fromtimestamp(now, timezone.utc).date()
                if day != self.day:
                    self.day, self.attempts = day, 0
                if self.attempts >= DAILY_ATTEMPTS:
                    raise DependencyError("daily_limit", "The sample's daily weather request limit was reached.", 429)
                key = self._key(now)
                self._check_deadline(self.clock())
                self.attempts += 1
                weather = curated_weather(self.fetch(key, location["coordinates"]))
                completed = self.clock()
                result = {
                    **weather,
                    "location": location["name"],
                    "fetchedAt": completed,
                    "cacheExpiresAt": completed + WEATHER_TTL,
                }
                self.weather[location["id"]] = result
                self.failures.pop(location["id"], None)
                return {**result, "cached": False}
            except DependencyError as error:
                self.failures[location["id"]] = (self.clock() + FAILURE_TTL, error)
                if error.code == "provider_auth":
                    self.secret = None
                    self.secret_until = 0
                raise
