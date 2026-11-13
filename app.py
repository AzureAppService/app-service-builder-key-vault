"""App Service Builder weather dashboard; no API credential is sent to visitors."""

import json
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone

from azure.core.exceptions import AzureError, ClientAuthenticationError, HttpResponseError, ResourceNotFoundError, ServiceRequestError
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
from flask import Flask, g, jsonify, render_template, request

from weather import DependencyError, WeatherService
from setup_page import setup_page

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


def requested_location(raw):
    message = "Enter a city or postal code using 2-80 characters, not a URL or IP address."
    if not isinstance(raw, str) or not 2 <= len(raw) <= 80:
        raise DependencyError("invalid_location", message, 400)
    value = unicodedata.normalize("NFKC", raw).strip(" ")
    if not 2 <= len(value) <= 80 or not all(
        character.isalpha() or character in "0123456789 ,.'-"
        for character in value
    ) or not any(character.isalnum() for character in value):
        raise DependencyError("invalid_location", message, 400)
    value = " ".join(value.split())
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value) or re.fullmatch(r"-?\d+(?:\.\d+)?,\s*-?\d+(?:\.\d+)?", value):
        raise DependencyError("invalid_location", message, 400)
    return {"id": "query:" + value.casefold(), "name": value, "query": value}


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
        except ClientAuthenticationError:
            raise DependencyError("identity_unavailable", "App identity could not authenticate. Check the same app's managed identity and redeployment.") from None
        except ServiceRequestError:
            raise DependencyError("vault_unreachable", "Key Vault is unreachable. Check the vault endpoint and network rules.") from None
        except HttpResponseError as error:
            if error.status_code in (401, 403):
                raise DependencyError("vault_denied", "Key Vault denied the read. Check the app identity's secret-read role and vault network rules.") from None
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
    configuration_error = None
    if service is None:
        try:
            loader = key_vault_loader()
            deadline = configured_deadline(os.environ.get("SAMPLE_EXPIRES_AT"))
        except ValueError:
            configuration_error = "configuration_missing" if not os.environ.get("KEY_VAULT_URL") or not os.environ.get("SAMPLE_EXPIRES_AT") else "configuration_invalid"

            def loader():
                raise DependencyError(configuration_error, "Weather setup is incomplete. Follow the setup steps, save settings and deploy a new version.")

            deadline = None
        service = WeatherService(loader, expires_at=deadline)
        if configuration_error:
            service.last_state = configuration_error
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

    @app.get("/README.md")
    def setup_readme():
        return setup_page()

    @app.get("/health")
    def health():
        return jsonify(status="ready")

    @app.get("/ready")
    def readiness():
        state = service.readiness()
        return jsonify(**state, setupUrl="./README.md#setup"), 200 if state["code"] == "weather_verified" else 503

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    def dependency_response(error):
        app.logger.warning("Weather request failed: category=%s", error.code)
        response = jsonify(error={"code": error.code, "message": str(error)})
        if error.retry_after is not None:
            response.headers["Retry-After"] = str(error.retry_after)
        return response, error.status

    def weather_response(location):
        try:
            return jsonify(service.read(location))
        except DependencyError as error:
            return dependency_response(error)

    @app.get("/api/weather/<identifier>")
    def weather(identifier):
        location = by_id.get(identifier)
        if location is None:
            return jsonify(error={"code": "location_not_found", "message": "Choose a configured location."}), 404
        return weather_response(location)

    @app.get("/api/weather")
    def lookup():
        try:
            if len(request.args.getlist("location")) != 1 or len(request.args) != 1:
                raise DependencyError("invalid_location", "Provide one city or postal code.", 400)
            location = requested_location(request.args["location"])
        except DependencyError as error:
            return dependency_response(error)
        return weather_response(location)

    return app


logging.getLogger("azure").setLevel(logging.WARNING)
app = create_app()
