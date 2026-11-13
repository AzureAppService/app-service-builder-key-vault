import concurrent.futures
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

os.environ.setdefault("KEY_VAULT_URL", "https://example-test-vault.vault.azure.net")
os.environ.setdefault("SAMPLE_EXPIRES_AT", "2030-01-01T00:00:00Z")

from app import DEFAULT_LOCATIONS, configured_deadline, configured_locations, create_app, key_vault_loader, requested_location
from weather import DAILY_ATTEMPTS, LOOKUP_ATTEMPTS_PER_MINUTE, MAX_CACHE_ENTRIES, MAX_RESPONSE_BYTES, DependencyError, WeatherService, fetch_weather


def secret(value="test-only-credential", expires=None, enabled=True):
    return SimpleNamespace(value=value, properties=SimpleNamespace(enabled=enabled, expires_on=expires))


def payload():
    return {"current": {"temp_c": 12, "feelslike_c": 10, "wind_kph": 8,
                        "humidity": 70, "last_updated_epoch": 1793847000,
                        "condition": {"text": "Partly cloudy"}},
            "location": {"name": "Seattle", "region": "Washington", "country": "USA"},
            "secret": "should-not-reach-browser"}


class WeatherTests(unittest.TestCase):
    def test_missing_configuration_boots_same_app_without_dependency_calls(self):
        with patch.dict(os.environ, {}, clear=True), patch("app.SecretClient") as vault, patch("weather.fetch_weather") as provider:
            client = create_app().test_client()
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertIn(b'id="setup"', client.get("/README.md").data)
            for path in ["/ready", "/api/weather/seattle"]:
                response = client.get(path)
                self.assertEqual(response.status_code, 503)
                body = response.get_json()
                self.assertEqual(body.get("code", body.get("error", {}).get("code")), "configuration_missing")
            vault.assert_not_called()
            provider.assert_not_called()

    def test_readiness_requires_actual_weather_and_expires_without_probes(self):
        client = create_app(self.service, DEFAULT_LOCATIONS).test_client()
        self.assertEqual(client.get("/ready").status_code, 503)
        self.assertEqual(self.loads, 0)
        self.assertEqual(client.get("/api/weather/seattle").status_code, 200)
        self.assertEqual(client.get("/ready").get_json()["code"], "weather_verified")
        self.assertEqual(self.loads, 1)
        self.assertEqual(len(self.keys), 1)
        self.now += 300
        self.assertEqual(client.get("/ready").status_code, 503)
        self.assertEqual(client.get("/ready").get_json()["code"], "not_checked")
        self.service.expires_at = self.now
        self.assertEqual(client.get("/ready").get_json()["code"], "demo_expired")
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(len(self.keys), 1)

    def test_vault_denied_unreachable_and_identity_failures_are_sanitized(self):
        from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ServiceRequestError
        for error, category in [
            (HttpResponseError("private-canary", response=SimpleNamespace(status_code=403, reason="Forbidden", headers={})), "vault_denied"),
            (ServiceRequestError("private-canary endpoint"), "vault_unreachable"),
            (ClientAuthenticationError("private-canary credential"), "identity_unavailable"),
        ]:
            with patch("app.SecretClient") as vault:
                vault.return_value.get_secret.side_effect = error
                with self.assertRaises(DependencyError) as raised:
                    key_vault_loader()()
                self.assertEqual(raised.exception.code, category)
                self.assertNotIn("private-canary", str(raised.exception))

    def setUp(self):
        self.now = 1793847000
        self.keys = []
        self.loads = 0
        self.value = secret()

        def load():
            self.loads += 1
            return self.value

        def fetch(key, coordinates):
            self.keys.append((key, coordinates))
            return payload()

        self.service = WeatherService(load, fetch, lambda: self.now)
        self.location = DEFAULT_LOCATIONS[0]

    def test_cache_expiry_and_rotated_secret(self):
        first = self.service.read(self.location)
        self.assertFalse(first["cached"])
        self.now += 599
        self.assertTrue(self.service.read(self.location)["cached"])
        self.assertEqual(len(self.keys), 1)
        self.value = secret("rotated-test-credential")
        self.now += 1
        self.assertFalse(self.service.read(self.location)["cached"])
        self.assertEqual(self.keys[-1][0], "rotated-test-credential")
        self.assertEqual(self.loads, 2)

    def test_secret_cache_shared_across_destinations(self):
        self.service.read(self.location)
        self.service.read(DEFAULT_LOCATIONS[1])
        self.assertEqual(self.loads, 1)
        self.assertEqual(len(self.keys), 2)

    def test_deadline_blocks_cached_and_new_weather_at_exact_threshold(self):
        self.service.expires_at = self.now + 10
        self.service.read(self.location)
        self.now += 9
        self.assertTrue(self.service.read(self.location)["cached"])
        self.now += 1
        for location in [self.location, DEFAULT_LOCATIONS[1]]:
            with self.assertRaises(DependencyError) as error:
                self.service.read(location)
            self.assertEqual(error.exception.code, "demo_expired")
            self.assertEqual(error.exception.status, 410)
        self.assertEqual(len(self.keys), 1)
        self.assertIsNone(self.service.secret)
        client = create_app(self.service, DEFAULT_LOCATIONS).test_client()
        self.assertEqual(client.get("/health").status_code, 200)
        response = client.get("/api/weather/seattle")
        self.assertEqual(response.status_code, 410)
        self.assertNotIn("Retry-After", response.headers)

    def test_deadline_rechecked_after_slow_secret_read(self):
        self.service.expires_at = self.now + 10

        def slow_load():
            self.now += 10
            return self.value

        self.service.load_secret = slow_load
        with self.assertRaises(DependencyError) as error:
            self.service.read(self.location)
        self.assertEqual(error.exception.code, "demo_expired")
        self.assertEqual(self.keys, [])

    def test_hosted_deadline_requires_valid_utc_timestamp(self):
        with patch.dict(os.environ, {"AZURE_AUTH_MODE": "managed-identity"}):
            for raw in [None, "", "invalid", "2026-11-05T12:00:00", "2026-11-05T12:00:00+03:00"]:
                with self.assertRaises(ValueError):
                    configured_deadline(raw)
            self.assertEqual(configured_deadline("2026-11-05T12:00:00Z"),
                             datetime(2026, 11, 5, 12, tzinfo=timezone.utc).timestamp())
        with patch.dict(os.environ, {"AZURE_AUTH_MODE": "azure-cli"}):
            self.assertIsNone(configured_deadline(None))

    def test_synthetic_preview_needs_no_hosted_deadline(self):
        environment = dict(os.environ)
        environment.pop("SAMPLE_EXPIRES_AT", None)
        environment.pop("AZURE_AUTH_MODE", None)
        code = (
            "import runpy; app = runpy.run_path('tests/preview.py')['preview']; "
            "response = app.test_client().get('/api/weather/seattle'); "
            "assert response.status_code == 200; "
            "assert 'synthetic data' in app.test_client().get('/').text"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_entered_location_is_normalized_and_resolved_by_provider(self):
        first = requested_location("  PARIS  ")
        second = requested_location("Paris")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(requested_location("New   York")["query"], "New York")
        self.assertEqual(requested_location("São Paulo")["query"], "São Paulo")
        response = self.service.read(first)
        self.assertEqual(response["location"], "Seattle, Washington, USA")
        self.assertTrue(self.service.read(second)["cached"])
        self.assertEqual(len(self.keys), 1)

    def test_invalid_entered_locations_never_reach_dependencies(self):
        client = create_app(self.service, DEFAULT_LOCATIONS).test_client()
        for value in ["", "x", "x" * 81, "https://example.com", "auto:ip", "id:2801268",
                      "127.0.0.1", "48.8,2.3", "Paris\nLondon", "<script>", "..."]:
            response = client.get("/api/weather", query_string={"location": value})
            self.assertEqual(response.status_code, 400, value)
        self.assertEqual(client.get("/api/weather?location=Paris&location=London").status_code, 400)
        self.assertEqual(client.get("/api/weather?location=Paris&url=other").status_code, 400)
        self.assertEqual(self.loads, 0)
        self.assertEqual(self.keys, [])

    def test_lookup_rate_limit_counts_only_uncached_entered_places(self):
        first = requested_location("City zero")
        self.service.read(first)
        for index in range(1, LOOKUP_ATTEMPTS_PER_MINUTE):
            self.service.read(requested_location(f"City {index}"))
        self.assertTrue(self.service.read(first)["cached"])
        self.service.read(self.location)
        with self.assertRaises(DependencyError) as error:
            self.service.read(requested_location("One more city"))
        self.assertEqual(error.exception.code, "lookup_limit")
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(error.exception.retry_after, 60)
        self.assertEqual(len(self.keys), LOOKUP_ATTEMPTS_PER_MINUTE + 1)
        self.now += 60
        self.service.read(requested_location("One more city"))
        self.assertEqual(len(self.keys), LOOKUP_ATTEMPTS_PER_MINUTE + 2)

    def test_location_cache_is_bounded_and_evicts_oldest_observation(self):
        for index in range(MAX_CACHE_ENTRIES + 1):
            self.service.read(requested_location(f"City {index}"))
            self.now += 6
        self.assertEqual(len(self.service.weather), MAX_CACHE_ENTRIES)
        count = len(self.keys)
        self.service.read(requested_location("City 0"))
        self.assertEqual(len(self.keys), count + 1)
        self.assertEqual(len(self.service.weather), MAX_CACHE_ENTRIES)

    def test_failed_query_cache_is_bounded(self):
        self.service.fetch = lambda *_: (_ for _ in ()).throw(DependencyError("location_not_found", "Not found.", 404))
        for index in range(MAX_CACHE_ENTRIES + 10):
            with self.assertRaises(DependencyError):
                self.service.read(requested_location(f"City {index}"))
        self.assertLessEqual(len(self.service.failures), MAX_CACHE_ENTRIES)
        self.assertEqual(self.service.attempts, LOOKUP_ATTEMPTS_PER_MINUTE)

    def test_slow_refresh_does_not_create_unbounded_lock_wait(self):
        with self.service.lock:
            with self.assertRaises(DependencyError) as error:
                self.service.read(self.location)
        self.assertEqual(error.exception.code, "refresh_busy")
        self.assertEqual(error.exception.retry_after, 1)
        self.assertEqual(self.loads, 0)

    def test_provider_auth_failure_cools_all_uncached_locations(self):
        self.service.fetch = lambda *_: (_ for _ in ()).throw(DependencyError("provider_auth", "Rejected."))
        for location in [self.location, DEFAULT_LOCATIONS[1]]:
            with self.assertRaises(DependencyError):
                self.service.read(location)
        self.assertEqual(self.service.attempts, 1)
        self.assertIsNone(self.service.secret)
        self.now += 60
        self.service.fetch = lambda *_: payload()
        self.service.read(DEFAULT_LOCATIONS[1])
        self.assertEqual(self.service.attempts, 2)
        self.assertEqual(self.loads, 2)

    def test_missing_provider_location_is_not_success_shaped(self):
        data = payload()
        del data["location"]
        self.service.fetch = lambda *_: data
        with self.assertRaises(DependencyError) as error:
            self.service.read(requested_location("Paris"))
        self.assertEqual(error.exception.code, "provider_response")

    def test_query_encoding_keeps_fixed_provider_destination(self):
        from urllib.parse import parse_qs, urlsplit
        with patch("weather.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = (
                json.dumps(payload()).encode()
            )
            fetch_weather("test-only-credential", "São Paulo, Brazil")
            url = urlsplit(opener.return_value.open.call_args.args[0].full_url)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.netloc, "api.weatherapi.com")
        self.assertEqual(url.path, "/v1/current.json")
        self.assertEqual(parse_qs(url.query)["q"], ["São Paulo, Brazil"])

    def test_provider_lookup_and_quota_errors_are_safe_and_specific(self):
        for http_status, provider_code, category, status in [
            (400, 1006, "location_not_found", 404),
            (403, 2007, "provider_quota", 429),
            (403, 2011, "provider_quota", 429),
        ]:
            body = io.BytesIO(json.dumps({"error": {"code": provider_code, "message": "private-key-in-error"}}).encode())
            with patch("weather.build_opener") as opener:
                opener.return_value.open.side_effect = HTTPError("https://api.weatherapi.com/?key=private-key-in-error", http_status, "", {}, body)
                with self.assertRaises(DependencyError) as error:
                    fetch_weather("private-key-in-error", "Unknown")
            self.assertEqual(error.exception.code, category)
            self.assertEqual(error.exception.status, status)
            self.assertNotIn("private-key-in-error", str(error.exception))

    def test_expired_secret_is_never_sent_to_provider(self):
        self.value = secret(expires=datetime.fromtimestamp(self.now - 1, timezone.utc))
        with self.assertRaises(DependencyError) as error:
            self.service.read(self.location)
        self.assertEqual(error.exception.code, "secret_expired")
        self.assertEqual(self.keys, [])

    def test_secret_cache_never_outlives_expiry(self):
        self.value = secret(expires=datetime.fromtimestamp(self.now + 10, timezone.utc))
        self.service.read(self.location)
        self.now += 11
        with self.assertRaises(DependencyError):
            self.service.read(DEFAULT_LOCATIONS[1])
        self.assertEqual(len(self.keys), 1)

    def test_disabled_and_empty_secrets_fail(self):
        for value, category in [(secret(enabled=False), "secret_disabled"), (secret(""), "secret_invalid")]:
            service = WeatherService(lambda: value, lambda *_: self.fail("Upstream should not be called"))
            with self.assertRaises(DependencyError) as error:
                service.read(self.location)
            self.assertEqual(error.exception.code, category)

    def test_failures_are_coalesced_and_not_success_shaped(self):
        self.service.fetch = lambda *_: (_ for _ in ()).throw(DependencyError("provider_unavailable", "Provider unavailable."))
        for _ in range(3):
            with self.assertRaises(DependencyError):
                self.service.read(self.location)
        self.assertEqual(self.service.attempts, 1)
        self.now += 60
        with self.assertRaises(DependencyError):
            self.service.read(self.location)
        self.assertEqual(self.service.attempts, 2)

    def test_concurrent_reads_make_one_provider_call(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.service.read(self.location), range(8)))
        self.assertEqual(len(self.keys), 1)
        self.assertEqual(sum(not item["cached"] for item in results), 1)

    def test_daily_cap_prevents_network_and_counts_failures(self):
        self.service.day = datetime.fromtimestamp(self.now, timezone.utc).date()
        self.service.attempts = DAILY_ATTEMPTS
        with self.assertRaises(DependencyError) as error:
            self.service.read(self.location)
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(self.keys, [])

    def test_unexpected_provider_values_fail(self):
        for value in [True, float("nan"), "12"]:
            data = payload()
            data["current"]["temp_c"] = value
            service = WeatherService(secret, lambda *_: data)
            with self.assertRaises(DependencyError):
                service.read(self.location)

    def test_api_does_not_return_credential_or_raw_provider_payload(self):
        client = create_app(self.service, DEFAULT_LOCATIONS).test_client()
        response = client.get("/api/weather/seattle", headers={"Host": "internal-adc.example"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("test-only-credential", response.text)
        self.assertNotIn("should-not-reach-browser", response.text)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(client.get("/api/weather/arbitrary").status_code, 404)
        self.assertEqual(client.post("/api/weather/seattle").status_code, 405)
        self.assertEqual(client.get("/health").json, {"status": "ready"})

    def test_ui_internal_host_and_output_escaping(self):
        locations = [{**self.location, "name": '<script>alert("x")</script>'}]
        client = create_app(self.service, locations).test_client()
        response = client.get("/", headers={"Host": "internal-adc.example"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('<script>alert("x")</script>', response.text)
        self.assertIn("&lt;script&gt;", response.text)

    def test_sdk_errors_are_redacted(self):
        from azure.core.exceptions import HttpResponseError
        with patch("app.SecretClient") as client:
            client.return_value.get_secret.side_effect = HttpResponseError("https://vault/?secret=do-not-leak")
            loader = key_vault_loader()
            with self.assertRaises(DependencyError) as error:
                loader()
            self.assertNotIn("do-not-leak", str(error.exception))

    def test_missing_secret_has_explicit_safe_error(self):
        from azure.core.exceptions import ResourceNotFoundError
        missing = ResourceNotFoundError("private SDK payload")
        missing.error = SimpleNamespace(code="SecretNotFound")
        with patch("app.SecretClient") as client:
            client.return_value.get_secret.side_effect = missing
            with self.assertRaises(DependencyError) as error:
                key_vault_loader()()
            self.assertEqual(error.exception.code, "secret_missing")
            self.assertNotIn("private SDK payload", str(error.exception))

    def test_provider_http_error_does_not_leak_key(self):
        with patch("weather.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError("https://api.weatherapi.com/?key=private-value", 401, "secret", {}, None)
            with self.assertRaises(DependencyError) as error:
                fetch_weather("private-value", "1,2")
            self.assertEqual(error.exception.code, "provider_auth")
            self.assertNotIn("private-value", str(error.exception))

    def test_response_size_limit_and_redirects(self):
        with patch("weather.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"x" * (MAX_RESPONSE_BYTES + 1)
            with self.assertRaises(DependencyError) as error:
                fetch_weather("test-only", "1,2")
            self.assertEqual(error.exception.code, "provider_response")
        from weather import NoRedirects
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "https://elsewhere.example"))

    def test_configuration_rejects_arbitrary_destination(self):
        for raw in ['[]', '[{"id":"x","name":"X","coordinates":"https://example.com"}]',
                    '[{"id":"x","name":"X","coordinates":"91,0"}]']:
            with self.assertRaises(ValueError):
                configured_locations(raw)


if __name__ == "__main__":
    unittest.main()
