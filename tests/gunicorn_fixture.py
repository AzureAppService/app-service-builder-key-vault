"""Worker-only external-service fixtures; never use this config in a deployment."""

import io
import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

SYNTHETIC_KEY = "gunicorn-smoke-only-not-a-real-key"
PRIVATE_FIELD = "raw-provider-field-must-not-leak"


def post_fork(server, worker):
    def load_secret(client, name, **kwargs):
        assert name == "weather-api-key"
        return SimpleNamespace(
            value=SYNTHETIC_KEY,
            properties=SimpleNamespace(enabled=True, expires_on=None),
        )

    def open_weather(request, timeout):
        url = urlsplit(request.full_url)
        query = parse_qs(url.query)
        assert (url.scheme, url.netloc, url.path) == (
            "https", "api.weatherapi.com", "/v1/current.json"
        )
        assert query["key"] == [SYNTHETIC_KEY]
        assert timeout == 8
        if query["q"] == ["Denied"]:
            raise HTTPError(request.full_url, 401, PRIVATE_FIELD, {}, io.BytesIO(b"{}"))
        return io.BytesIO(json.dumps({
            "current": {
                "temp_c": 12, "feelslike_c": 10, "wind_kph": 8, "humidity": 70,
                "last_updated_epoch": 1793847000,
                "condition": {"text": "Partly cloudy"},
            },
            "location": {"name": "Paris", "region": "", "country": "France"},
            "private": PRIVATE_FIELD,
        }).encode())

    patch("azure.keyvault.secrets.SecretClient.get_secret", load_secret).start()
    patch("weather.build_opener", return_value=SimpleNamespace(open=open_weather)).start()
