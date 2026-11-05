"""Real manifest-command/HTTP smoke with synthetic external-service boundaries."""

import concurrent.futures
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


class GunicornTests(unittest.TestCase):
    def test_manifest_startup_requests_and_graceful_shutdown(self):
        commands = [
            line.strip().removeprefix("start: ")
            for line in (ROOT / "builder.yaml").read_text().splitlines()
            if line.strip().startswith("start: ")
        ]
        self.assertEqual(len(commands), 1)
        command = shlex.split(commands[0])
        self.assertEqual(command[0], "gunicorn")
        self.assertEqual(command[-1], "app:app")

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryFile(mode="w+") as log:
            environment = {
                "PATH": os.environ["PATH"],
                "HOME": home,
                "PYTHONPATH": str(ROOT),
                "KEY_VAULT_URL": "https://example-test-vault.vault.azure.net",
                "KEY_VAULT_SECRET_NAME": "weather-api-key",
                "AZURE_AUTH_MODE": "managed-identity",
                "SAMPLE_EXPIRES_AT": (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            }
            # Inherit a reserved loopback listener instead of racing for a free port.
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                command[command.index("--bind") + 1] = f"fd://{listener.fileno()}"
                arguments = [
                    sys.executable, "-m", "gunicorn", *command[1:],
                    "--config", str(ROOT / "tests/gunicorn_fixture.py"),
                ]
                configuration = subprocess.run(
                    [*arguments, "--print-config"],
                    cwd=ROOT, env=environment, capture_output=True, text=True,
                    pass_fds=(listener.fileno(),), timeout=10,
                )
                self.assertEqual(configuration.returncode, 0, configuration.stderr)
                settings = {
                    name.strip(): value.strip()
                    for line in configuration.stdout.splitlines()
                    for name, separator, value in [line.partition(" = ")]
                    if separator
                }
                for name, expected in {
                    "workers": "1", "threads": "4", "timeout": "30",
                    "preload_app": "False",
                }.items():
                    self.assertEqual(settings[name], expected, name)
                process = subprocess.Popen(
                    arguments,
                    cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT,
                    pass_fds=(listener.fileno(),), start_new_session=True,
                )

            def request(path, method="GET"):
                opener = build_opener(ProxyHandler({}))
                try:
                    response = opener.open(
                        Request(f"http://127.0.0.1:{port}{path}", method=method),
                        timeout=2,
                    )
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, response.headers, response.read().decode()

            def json_request(path, status=200):
                actual, headers, body = request(path)
                self.assertEqual(actual, status, body)
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
                self.assertNotIn("gunicorn-smoke-only-not-a-real-key", body)
                self.assertNotIn("raw-provider-field-must-not-leak", body)
                return headers, json.loads(body)

            try:
                deadline = time.monotonic() + 15
                while True:
                    self.assertIsNone(process.poll(), "Gunicorn exited before readiness")
                    try:
                        self.assertEqual(json_request("/health")[1], {"status": "ready"})
                        break
                    except URLError:
                        if time.monotonic() >= deadline:
                            self.fail("Gunicorn did not become ready within 15 seconds")
                        time.sleep(0.05)

                status, _, page = request("/")
                self.assertEqual(status, 200)
                self.assertIn("Seattle", page)
                self.assertEqual(request("/static/app.js")[0], 200)
                first = json_request("/api/weather/seattle")[1]
                self.assertEqual(first["temperature"], 12)
                self.assertEqual(first["location"], "Seattle")
                self.assertEqual(first["observedAt"], 1793847000)
                self.assertEqual(first["cacheExpiresAt"] - first["fetchedAt"], 600)
                self.assertFalse(first["cached"])
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                    cached = list(executor.map(
                        lambda _: json_request("/api/weather/seattle")[1], range(8)
                    ))
                for item in cached:
                    self.assertEqual(item, {**first, "cached": True})
                self.assertEqual(
                    json_request("/api/weather?location=Paris")[1]["location"],
                    "Paris, France",
                )
                self.assertEqual(
                    json_request("/api/weather?location=https://example.com", 400)[1]["error"]["code"],
                    "invalid_location",
                )
                self.assertEqual(
                    json_request("/api/weather/unknown", 404)[1]["error"]["code"],
                    "location_not_found",
                )
                self.assertEqual(request("/api/weather/seattle", method="POST")[0], 405)
                headers, error = json_request("/api/weather?location=Denied", 503)
                self.assertEqual(error["error"]["code"], "provider_auth")
                self.assertEqual(headers["Retry-After"], "60")
                self.assertEqual(
                    json_request("/api/weather/london", 503)[1]["error"]["code"],
                    "provider_auth",
                )
                self.assertEqual(json_request("/health")[1], {"status": "ready"})
                process.terminate()
                self.assertEqual(process.wait(timeout=10), 0)
                log.seek(0)
                output = log.read()
                self.assertIn("Using worker: gthread", output)
                self.assertEqual(output.count("Booting worker with pid:"), 1)
                self.assertIn("Worker exiting", output)
                self.assertIn("Shutting down: Master", output)
                self.assertNotIn("gunicorn-smoke-only-not-a-real-key", output)
                self.assertNotIn("raw-provider-field-must-not-leak", output)
            except BaseException as error:
                log.seek(0)
                error.add_note("Gunicorn output:\n" + log.read())
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
