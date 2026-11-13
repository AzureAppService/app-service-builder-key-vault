"""Local UI fixture only; never a hosted integration or provider fallback."""

import os
import argparse
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("KEY_VAULT_URL", "https://example-test-vault.vault.azure.net")
os.environ["AZURE_AUTH_MODE"] = "azure-cli"

from app import DEFAULT_LOCATIONS, create_app
from weather import DependencyError, WeatherService

missing_secret = False

def load_secret():
    if missing_secret:
        raise DependencyError("secret_missing", "The configured secret does not exist. Add it directly to the dedicated vault.")
    return SimpleNamespace(value="synthetic-preview-only", properties=SimpleNamespace(enabled=True, expires_on=None))


def fetch(key, coordinates):
    temperatures = {"47.6062,-122.3321": 12, "51.5074,-0.1278": 10, "1.3521,103.8198": 28}
    if coordinates in temperatures:
        temperature = temperatures[coordinates]
    elif coordinates.casefold() in ("paris", "paris, france"):
        temperature = 14
    else:
        raise DependencyError("location_not_found", "The synthetic preview has a Paris fixture and the three quick picks.", 404)
    return {"location": {"name": "Paris", "region": "Ile-de-France", "country": "France"},
            "current": {"temp_c": temperature, "feelslike_c": temperature - 2,
                        "wind_kph": 12, "humidity": 68,
                        "last_updated_epoch": int(time.time()),
                        "condition": {"text": "Partly cloudy"}}}


preview = create_app(WeatherService(load_secret, fetch), DEFAULT_LOCATIONS)
preview.jinja_env.globals["preview_notice"] = "Local UI preview - synthetic data. Azure and WeatherAPI are not contacted."

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8117)
    parser.add_argument("--missing-secret", action="store_true")
    args = parser.parse_args()
    missing_secret = args.missing_secret
    if missing_secret:
        preview.jinja_env.globals["preview_notice"] = "Local UI preview - missing-secret fixture. Azure and WeatherAPI are not contacted."
    preview.run(host="127.0.0.1", port=args.port)
