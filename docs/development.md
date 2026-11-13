# Local weather checks

Use the installed Python 3.14 environment. From the repository root:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python tests/preview.py --port 8117
# Or an explicitly simulated missing secret:
.venv/bin/python tests/preview.py --port 8117 --missing-secret
```

The fixture binds loopback and substitutes both secret and weather boundaries. Its banner states that Azure and WeatherAPI are not contacted. A synthetic successful card or missing-secret fixture is not live integration evidence.

For existing Playwright UI checks, point `PLAYWRIGHT_MODULE` to your installed module and `EDGE_EXECUTABLE` to installed Edge:

```bash
SAMPLE_URL=http://127.0.0.1:8117 node tests/ui.mjs
# For the missing-secret fixture add PREVIEW_SECRET_MISSING=1.
```

For actual local Key Vault access, sign in with Azure CLI and use `AZURE_AUTH_MODE=azure-cli`, `KEY_VAULT_URL` and `KEY_VAULT_SECRET_NAME`. Hosted runs explicitly use managed identity and an absolute `SAMPLE_EXPIRES_AT`; do not use local credential mode in Builder. Never log or paste credential values.

## Cache, expiry and limits

The server caches each curated weather observation for ten minutes and the secret for at most five minutes or its expiry, whichever is earlier. Secret/provider failures are cached for sixty seconds. Cached data is labeled; no stale success is served after expiry.

One worker/four threads serializes refreshes with a one-second lock timeout. Up to 64 weather/query entries are retained, with 500 provider attempts per UTC day and 12 new place lookups per minute. These are per-process limits, not an account budget. The fixed HTTPS provider call has an eight-second timeout, no redirects and a 64 KiB response cap. No automatic retries or configurable provider URLs are supported.

An expired/disabled/missing/invalid secret stops reads. Provider auth failure clears the cached key; rotate directly in Key Vault and allow the failure-cache interval. Demo expiry clears the secret and blocks both cached and new requests with 410, while `/` and `/health` remain readable.

## Location configuration

`WEATHER_LOCATIONS` accepts one to three objects with unique lowercase IDs, readable names and valid latitude/longitude:

```json
[{"id":"seattle","name":"Seattle","coordinates":"47.6062,-122.3321"}]
```

The browser accepts a city/postal code, never a URL/IP/coordinate endpoint. Provider matching can be ambiguous: inspect the returned place. Unit conversion is browser-only and does not make another provider request. Sanitize all values before adding other data sources.

The [post-deployment setup](../README.md#setup) is the primary journey. The vault template creates no web app. No model/resource picker, automatic provisioning, credential input or cloud configuration editor exists in the runtime.
