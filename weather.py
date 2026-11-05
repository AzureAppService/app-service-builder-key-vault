"""Bounded weather reads with a server-only, expiring Key Vault credential."""

import json
import math
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

WEATHER_TTL = 600
SECRET_TTL = 300
FAILURE_TTL = 60
DAILY_ATTEMPTS = 500
MAX_RESPONSE_BYTES = 64 * 1024
MAX_CACHE_ENTRIES = 64
LOOKUP_ATTEMPTS_PER_MINUTE = 12


class DependencyError(Exception):
    def __init__(self, code, message, status=503, retry_after=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after if retry_after is not None else (60 if status == 503 else None)


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
        try:
            body = error.read(MAX_RESPONSE_BYTES + 1)
            provider_code = json.loads(body)["error"]["code"] if len(body) <= MAX_RESPONSE_BYTES else None
        except (ValueError, UnicodeError, KeyError, TypeError, OSError):
            provider_code = None
        finally:
            error.close()
        if provider_code == 1006:
            raise DependencyError("location_not_found", "No matching place was found. Try a different city or postal code.", 404) from None
        if provider_code in (2007, 2011):
            raise DependencyError("provider_quota", "WeatherAPI's call allowance was reached. The app owner must review provider usage.", 429) from None
        if error.code in (401, 403):
            raise DependencyError("provider_auth", "Weather provider rejected the configured credential or its access rules.") from None
        raise DependencyError("provider_unavailable", "Weather provider rejected the request.") from None
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


def resolved_location(payload):
    try:
        location = payload["location"]
        name, region, country = location["name"], location.get("region", ""), location["country"]
        if not all(isinstance(part, str) and len(part) <= 100 for part in (name, region, country)) or not name.strip() or not country.strip():
            raise ValueError
        return ", ".join(dict.fromkeys(part.strip() for part in (name, region, country) if part.strip()))
    except (KeyError, TypeError, ValueError):
        raise DependencyError("provider_response", "Weather provider returned an unexpected location.") from None


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
        self.lookup_attempts = deque()
        self.provider_failure = None
        self.provider_retry_at = 0

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

    def _remember(self, cache, identifier, value):
        cache.pop(identifier, None)
        if len(cache) >= MAX_CACHE_ENTRIES:
            cache.pop(next(iter(cache)))
        cache[identifier] = value

    def read(self, location):
        if not self.lock.acquire(timeout=1):
            raise DependencyError("refresh_busy", "Another weather refresh is in progress. Try again shortly.", retry_after=1)
        try:
            now = self.clock()
            self._check_deadline(now)
            for identifier, cached in tuple(self.weather.items()):
                if now >= cached["cacheExpiresAt"]:
                    del self.weather[identifier]
            for identifier, failure in tuple(self.failures.items()):
                if now >= failure[0]:
                    del self.failures[identifier]
            cached = self.weather.get(location["id"])
            if cached and now < cached["cacheExpiresAt"]:
                return {**cached, "cached": True}
            failure = self.failures.get(location["id"])
            if failure and now < failure[0]:
                raise failure[1]
            if self.provider_failure and now < self.provider_retry_at:
                raise self.provider_failure
            try:
                day = datetime.fromtimestamp(now, timezone.utc).date()
                if day != self.day:
                    self.day, self.attempts = day, 0
                if self.attempts >= DAILY_ATTEMPTS:
                    raise DependencyError("daily_limit", "The sample's daily weather request limit was reached.", 429)
                if "query" in location:
                    while self.lookup_attempts and now >= self.lookup_attempts[0] + 60:
                        self.lookup_attempts.popleft()
                    if len(self.lookup_attempts) >= LOOKUP_ATTEMPTS_PER_MINUTE:
                        raise DependencyError("lookup_limit", "Too many new place lookups. Wait a minute or choose a cached destination.", 429, retry_after=60)
                    self.lookup_attempts.append(now)
                key = self._key(now)
                self._check_deadline(self.clock())
                self.attempts += 1
                payload = self.fetch(key, location.get("query") or location["coordinates"])
                weather = curated_weather(payload)
                completed = self.clock()
                result = {
                    **weather,
                    "location": resolved_location(payload) if "query" in location else location["name"],
                    "fetchedAt": completed,
                    "cacheExpiresAt": completed + WEATHER_TTL,
                }
                self._remember(self.weather, location["id"], result)
                self.provider_failure = None
                self.failures.pop(location["id"], None)
                return {**result, "cached": False}
            except DependencyError as error:
                self._remember(self.failures, location["id"], (self.clock() + FAILURE_TTL, error))
                if error.code in ("provider_auth", "provider_quota", "provider_unavailable"):
                    self.provider_failure = error
                    self.provider_retry_at = self.clock() + FAILURE_TTL
                if error.code == "provider_auth":
                    self.secret = None
                    self.secret_until = 0
                raise
        finally:
            self.lock.release()
