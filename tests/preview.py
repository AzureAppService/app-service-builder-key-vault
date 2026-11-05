"""Local UI fixture only; never a hosted integration or provider fallback."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("KEY_VAULT_URL", "https://example-test-vault.vault.azure.net")
os.environ["AZURE_AUTH_MODE"] = "azure-cli"

from app import DEFAULT_LOCATIONS, create_app
from weather import WeatherService


def load_secret():
    return SimpleNamespace(value="synthetic-preview-only", properties=SimpleNamespace(enabled=True, expires_on=None))


def fetch(key, coordinates):
    temperature = {"47.6062,-122.3321": 12, "51.5074,-0.1278": 10, "1.3521,103.8198": 28}[coordinates]
    return {"current": {"temp_c": temperature, "feelslike_c": temperature - 2,
                        "wind_kph": 12, "humidity": 68,
                        "last_updated_epoch": int(time.time()),
                        "condition": {"text": "Partly cloudy"}}}


preview = create_app(WeatherService(load_secret, fetch), DEFAULT_LOCATIONS)
preview.jinja_env.globals["preview_notice"] = "Local UI preview - synthetic data. Azure and WeatherAPI are not contacted."

if __name__ == "__main__":
    preview.run(host="127.0.0.1", port=8117)
