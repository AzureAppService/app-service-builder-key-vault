"""App Service Builder weather dashboard; no API credential is sent to visitors."""

import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
from flask import Flask, g, jsonify, render_template

from weather import DependencyError, WeatherService

DEFAULT_LOCATIONS = [
    {"id": "seattle", "name": "Seattle", "coordinates": "47.6062,-122.3321"},
    {"id": "london", "name": "London", "coordinates": "51.5074,-0.1278"},
    {"id": "singapore", "name": "Singapore", "coordinates": "1.3521,103.8198"},
]


def configured_locations(raw):
    locations = json.loads(raw) if raw else DEFAULT_LOCATIONS
    if not isinstance(locations, list) or not 1 <= len(locations) <= 3:
        raise ValueError("WEATHER_LOCATIONS must contain one to three locations.")
    ids = set()
    for location in locations:
        if not isinstance(location, dict) or set(location) != {"id", "name", "coordinates"}:
            raise ValueError("Each location needs id, name, and coordinates.")
        identifier, name, coordinates = location["id"], location["name"], location["coordinates"]
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", identifier) or identifier in ids:
            raise ValueError("Location IDs must be unique lowercase identifiers.")
        if not isinstance(name, str) or not 1 <= len(name) <= 60:
            raise ValueError("Location names must contain 1-60 characters.")
        if not isinstance(coordinates, str) or not re.fullmatch(r"-?\d{1,3}(?:\.\d{1,6})?,-?\d{1,3}(?:\.\d{1,6})?", coordinates):
            raise ValueError("Coordinates must be latitude,longitude.")
        lat, lon = map(float, coordinates.split(","))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Coordinates are outside latitude/longitude ranges.")
        ids.add(identifier)
    return locations


def key_vault_loader():
    vault_url = os.environ.get("KEY_VAULT_URL", "")
    secret_name = os.environ.get("KEY_VAULT_SECRET_NAME", "weather-api-key")
    if not re.fullmatch(r"https://[a-zA-Z0-9-]{3,24}\.vault\.azure\.net/?", vault_url):
        raise ValueError("KEY_VAULT_URL must be a public Azure Key Vault HTTPS URL.")
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,127}", secret_name):
        raise ValueError("KEY_VAULT_SECRET_NAME is invalid.")
    mode = os.environ.get("AZURE_AUTH_MODE", "managed-identity")
    if mode not in ("managed-identity", "azure-cli"):
        raise ValueError("AZURE_AUTH_MODE must be managed-identity or azure-cli.")
    credential = (
        AzureCliCredential(process_timeout=5)
        if mode == "azure-cli"
        else ManagedIdentityCredential(retry_total=0, connection_timeout=3, read_timeout=5)
    )
    client = SecretClient(vault_url, credential, retry_total=0, connection_timeout=3, read_timeout=5)

    def load():
        try:
            return client.get_secret(secret_name, logging_enable=False)
        except ResourceNotFoundError as error:
            if getattr(error.error, "code", None) == "SecretNotFound":
                raise DependencyError("secret_missing", "The configured secret does not exist. Add it directly to the dedicated vault.") from None
            raise DependencyError("vault_access", "Cannot read the configured Key Vault secret. Check identity, permissions, and connectivity.") from None
        except AzureError:
            raise DependencyError("vault_access", "Cannot read the configured Key Vault secret. Check identity, permissions, and connectivity.") from None

    return load


def configured_deadline(raw):
    if not raw:
        if os.environ.get("AZURE_AUTH_MODE", "managed-identity") == "azure-cli":
            return None
        raise ValueError("SAMPLE_EXPIRES_AT is required for hosted runs.")
    try:
        deadline = datetime.fromisoformat(raw)
        if deadline.tzinfo is None or deadline.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError
        return deadline.timestamp()
    except (ValueError, OverflowError):
        raise ValueError("SAMPLE_EXPIRES_AT must be an ISO 8601 UTC timestamp.") from None


def create_app(service=None, locations=None):
    app = Flask(__name__)
    locations = locations if locations is not None else configured_locations(os.environ.get("WEATHER_LOCATIONS"))
    service = service if service is not None else WeatherService(
        key_vault_loader(), expires_at=configured_deadline(os.environ.get("SAMPLE_EXPIRES_AT"))
    )
    by_id = {location["id"]: location for location in locations}

    @app.before_request
    def nonce():
        g.nonce = secrets.token_urlsafe(24)

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self' 'nonce-{g.nonce}'; "
            f"style-src 'self' 'nonce-{g.nonce}'; img-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/")
    def index():
        return render_template("index.html", locations=locations)

    @app.get("/health")
    def health():
        return jsonify(status="ready")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/api/weather/<identifier>")
    def weather(identifier):
        location = by_id.get(identifier)
        if location is None:
            return jsonify(error={"code": "location_not_found", "message": "Choose a configured location."}), 404
        try:
            return jsonify(service.read(location))
        except DependencyError as error:
            app.logger.warning("Weather request failed: category=%s", error.code)
            response = jsonify(error={"code": error.code, "message": str(error)})
            if error.status == 503:
                response.headers["Retry-After"] = "60"
            return response, error.status

    return app


logging.getLogger("azure").setLevel(logging.WARNING)
app = create_app()
