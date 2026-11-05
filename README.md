# Call a weather API with a Key Vault secret

Check current weather for a city or postal code, or use three configured quick
picks, in a small Python application. The server reads a private WeatherAPI.com credential from Azure
Key Vault using managed identity, then calls WeatherAPI over HTTPS. The browser
receives curated observations, not the credential.

This demonstrates a real credential-based dependency that cannot use Microsoft
Entra authentication. A keyless weather API would not need Key Vault.

Canonical source: [AzureAppService/app-service-builder-key-vault](https://github.com/AzureAppService/app-service-builder-key-vault).

## Prerequisites

- An Azure subscription authorized for App Service Builder in West US 2, with
  Python 3.14 support. Use development/test only.
- Azure CLI with Bicep and permission to create a new vault and assign roles.
- A GitHub fork containing this source, with Builder GitHub App access.
- A WeatherAPI.com account and a private key reserved for this app. Review the
  provider's terms yourself. Its advertised Free offer is 100,000 calls/month
  without a card; signup starts a seven-day trial that automatically moves to
  Free. This app uses only current-weather fields.
- Public HTTPS access from Builder to the new vault and WeatherAPI. Builder
  does not provide customer VNet integration or stable egress IPs. Stop if your
  policy requires a private-only vault rather than weakening another vault.

Anyone who reaches the app can request weather. Caching and the per-process
daily allowance are usage controls, not authentication or an account-wide
spending cap. Use an isolated test group and a short review window. Hosted
startup requires `SAMPLE_EXPIRES_AT`, an absolute UTC deadline that survives
restarts and scale to zero. After that deadline the UI remains available, but
weather requests return an explicit test-window-ended error.

## 1. Prepare the vault

From the sample root, define your own values:

```bash
SUBSCRIPTION="<subscription-id>"
TENANT="<tenant-id>"
RG="<new-test-resource-group>"
VAULT="<globally-unique-vault-name>"
LOCATION="westus2"

az login --tenant "$TENANT"
az group create --subscription "$SUBSCRIPTION" --name "$RG" --location "$LOCATION"
az deployment group what-if --subscription "$SUBSCRIPTION" \
  --resource-group "$RG" --name weather-vault \
  --template-file infra/main.bicep --parameters vaultName="$VAULT"
```

Review changes and Key Vault charges, then provision:

```bash
az deployment group create --subscription "$SUBSCRIPTION" \
  --resource-group "$RG" --name weather-vault \
  --template-file infra/main.bicep --parameters vaultName="$VAULT"
VAULT_ID=$(az keyvault show --subscription "$SUBSCRIPTION" \
  --resource-group "$RG" --name "$VAULT" --query id -o tsv)
VAULT_URL=$(az keyvault show --subscription "$SUBSCRIPTION" \
  --resource-group "$RG" --name "$VAULT" --query properties.vaultUri -o tsv)
OPERATOR_ID=$(az ad signed-in-user show --query id -o tsv)
az role assignment create --subscription "$SUBSCRIPTION" \
  --assignee-object-id "$OPERATOR_ID" --assignee-principal-type User \
  --role "Key Vault Secrets Officer" --scope "$VAULT_ID"
```

In Azure Portal, open **your new vault > Objects > Secrets > Generate/Import**.
Choose manual entry, name the secret `weather-api-key`, enter your private
WeatherAPI key, enable it, and set an expiry that matches your intended test
window. Select **Create**. Do not put the key in a command, Git, app settings,
chat, screenshots, or browser assets.

The vault is Standard, RBAC-enabled, with a public endpoint. Public network
reachability does not permit anonymous secret reads. It has seven-day
soft-delete retention and purge protection; deletion does not immediately
release its name.

## 2. Create the Builder app

1. In Builder Portal, create an app using **Use my repository**, your authorized
   fork/branch, repository root `.`, your test group, and **West US 2**.
2. In the create wizard's **Access** step, choose public visitor access and
   enable the system-assigned managed identity. If identity is configured
   afterward, open **Identity**, enable it, and use **Save and deploy**.
3. On **Variables**, set `KEY_VAULT_URL` to the recorded HTTPS vault URL and
   `KEY_VAULT_SECRET_NAME` to `weather-api-key`. Do not set the API key itself.
   Set `SAMPLE_EXPIRES_AT` to your short test window's end in UTC, such as
   `2026-11-05T12:00:00Z`, replacing that example with your own future deadline.
   Set the vault secret's expiry to that same deadline. Leave `AZURE_CLIENT_ID`
   unset and do not enable local `AZURE_AUTH_MODE=azure-cli`.
4. Record the identity's **principal ID** from **Identity**. It is not the
   client ID or Builder app resource ID.

The app requires its nonsecret vault URL at startup; an initial deployment
without settings can fail. Finish settings/permissions and redeploy the same
app rather than creating a duplicate.

## 3. Grant secret reads and deploy

```bash
APP_PRINCIPAL_ID="<system-assigned-identity-principal-id>"
az role assignment create --subscription "$SUBSCRIPTION" \
  --assignee-object-id "$APP_PRINCIPAL_ID" --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" --scope "$VAULT_ID"
```

This dedicated vault holds only this app's key. The application identity can
read secrets; it cannot set or rotate them. Role propagation can take time.
Do not substitute Owner, Contributor, or Secrets Officer for the app's role.

Use **Save and deploy** for changed variables, then explicitly deploy the
selected source revision. The manifest installs root `requirements.txt` using
the Python build, starts one Gunicorn worker with four threads on port 8080,
and uses `/health` for process readiness. Verify dependency installation in
build logs on your target.

For a bounded demo, set **Scaling** minimum replicas to **0** and maximum to
**1**. This enables scale to zero and cold starts. Builder requires maximum
greater than minimum; minimum 1/maximum 1 is not valid.

## 4. Verify

After deployment succeeds, use the returned runtime URL:

```bash
APP_URL="<returned-app-runtime-url>"
curl --fail --silent --show-error "$APP_URL/health"
curl --fail --silent --show-error "$APP_URL/api/weather/seattle"
```

Health returns `{"status":"ready"}`; it does not prove vault or weather access.
The second request must return real weather fields, including `observedAt`,
`fetchedAt`, and `cached`. Repeat it within ten minutes and expect
`"cached":true`, without a secret field. Open the app, select each destination,
and check observation timestamps and visible units.

Enter a city such as `Paris`, a US ZIP such as `10001`, or a supported UK/Canadian
postal code, then use **Get weather**. Verify the returned place: provider
matching can be ambiguous. Invalid input, a place that cannot be found, a busy
refresh, and exhausted app/provider allowances have explicit errors.

```bash
curl --fail --silent --show-error --get "$APP_URL/api/weather" \
  --data-urlencode "location=Paris"
```

Switch **Units** between Celsius and Fahrenheit. Temperature and feels-like
change between °C/°F; wind changes between km/h/mph. The browser converts the
already loaded observation, without another API or Key Vault request.

Verify the active version/source revision in Builder. Inspect browser network
responses and sanitized runtime diagnostics: the key must not appear.
App-to-vault identity, data-role permissions, networking, and provider
authentication are separate checks. A mock or local CLI login does not prove
hosted managed-identity discovery.

## Cache and rotation behavior

- Weather is cached for ten minutes per configured destination or normalized
  entered query. At most 64 observations and 64 short-lived failures are held
  per process; oldest stored entries are evicted when full. There is no manual
  bypass. Different query spellings can resolve to the same city and still
  consume separate calls.
- At most 12 uncached entered-place lookups are accepted in any rolling minute
  per process. Quick picks and cache hits do not consume that lookup allowance.
  All actual provider attempts still share the 500-attempt UTC-day allowance.
  One upstream refresh runs at a time; requests waiting over one second fail
  explicitly rather than creating an unbounded queue.
- The latest unversioned secret is cached for up to five minutes and never
  beyond its recorded expiry. Disabled/expired/empty secrets fail explicitly.
- Create a new version of `weather-api-key` when you rotate the provider key.
  After the secret cache expires, new upstream calls use the new version.
  Provider authentication failure invalidates the cached key and triggers a
  one-minute cooldown. Previously fetched weather can remain until its
  ten-minute cache expires; there is no instantaneous-revocation claim.
- The process permits at most 500 weather attempts per UTC day, including
  failed calls. Restarts, scale to zero, and additional workers reset or
  multiply this in-memory allowance. It is not a distributed quota.
- No provider call is started at or after `SAMPLE_EXPIRES_AT`, including after
  restarts. A request already in flight can finish after the deadline.
  Cached weather is not returned after the deadline. Extending the window
  requires an owner-approved variable change and deployment; it does not
  extend the provider key or vault secret's validity.
- At steady state three continually requested locations need about 432 calls
  per day. Provider errors, process restarts, and other use of the account
  alter that figure. Review your account's actual usage.
- An uncached entered location uses the same fixed current-weather endpoint,
  not a separate geocoding service. Additional queries and cache eviction
  increase provider use. These in-memory controls do not impose an account-wide
  quota or spending cap and can reset on restarts/scale to zero.

For custom destinations, use a `WEATHER_LOCATIONS` variable containing up to
three objects with unique `id`, display `name`, and numeric `coordinates`:

```json
[{"id":"seattle","name":"Seattle","coordinates":"47.6062,-122.3321"}]
```

Deploy a new version to capture changed variables.
Entered queries are length-limited, normalized, and encoded. URLs, IP addresses,
coordinate input, and provider-specific lookup prefixes are not accepted.
Only the server uses the fixed `api.weatherapi.com` destination. Application
links are relative and do not depend on the public Host being passed through
Builder's proxy.

## Local development

```bash
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
export KEY_VAULT_URL="https://<your-vault-name>.vault.azure.net"
export KEY_VAULT_SECRET_NAME="weather-api-key"
export AZURE_AUTH_MODE="azure-cli"
gunicorn --bind 127.0.0.1:8080 --workers 1 --threads 4 app:app
```

Your signed-in local user needs secret-read permission. Hosted mode defaults
to `ManagedIdentityCredential`; it does not fall back to a local user.
Tests use synthetic keys and provider payloads. They do not contact Azure or
qualify a hosted deployment.

## Clean up resources

After review, delete the Builder app in **Settings > Danger zone**, remove
the operator role assignment you added, and delete only your isolated group:

```bash
az role assignment delete --subscription "$SUBSCRIPTION" \
  --assignee "$OPERATOR_ID" --role "Key Vault Secrets Officer" --scope "$VAULT_ID"
az group delete --subscription "$SUBSCRIPTION" --name "$RG"
```

Confirm that the group contains only this exercise's resources first.
Purge protection retains the deleted vault for seven days; do not bypass it
or delete another vault to reuse a name. Revoke/regenerate the test provider
key in your WeatherAPI account once it is no longer needed. Azure deletion
does not close a provider account or cancel a paid subscription.

## Related content

- [Builder managed identity](https://builder.appservice.azure.com/docs/guides/managed-identity/)
- [Key Vault secret permissions](https://learn.microsoft.com/azure/key-vault/general/rbac-guide)
- [WeatherAPI authentication and terms](https://www.weatherapi.com/docs/authentication/)
