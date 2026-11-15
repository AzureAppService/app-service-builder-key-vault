# Configure weather with CLI or ARM

Use a dedicated DEV/TEST group, explicit subscription and approved costs/deadline.
Run from the fork's root; creation templates are not migration updates for an existing app.

## Provision the vault, app and secret-read role

Set `SUBSCRIPTION`, `RG`, unique new `APP`/`VAULT`, and owner-approved future UTC `EXPIRES_AT`.
After Azure CLI sign-in and dedicated group creation:

```bash
az deployment group create --subscription "$SUBSCRIPTION" -g "$RG" \
  --name weather-vault --template-file infra/main.bicep --parameters vaultName="$VAULT" location=westus2
az deployment group create --subscription "$SUBSCRIPTION" -g "$RG" \
  --name weather-app --template-file infra/app.bicep \
  --parameters appName="$APP" vaultName="$VAULT" expiresAt="$EXPIRES_AT"
az rest --method get --url "https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RG/providers/Microsoft.Web/builderApps/$APP?api-version=2026-09-01-preview" \
  --query '{identity:identity,identityUpdate:properties.identityUpdate}'
```

The app template creates only a source-less SAMI app, 0–1 scaling, expiry control and vault-scoped Secrets User.
Wait for committed identity/no pending operation (`builder app status`) and role propagation.
With ARM-only tooling, GET the app ARM URL below and inspect `identity` and `properties.identityUpdate` instead.
An authorized vault owner must separately store the real WeatherAPI credential under `weather-api-key` using README setup.
Never put it in connection JSON, app variables, command arguments or screenshots.
Copy root `connection.json` into private ignored `.local/connection.json`, replacing vault ID/URI and secret name.
Prepare it with `umask 077; mkdir -p .local; cp connection.json .local/connection.json`.
Keep `KEY_VAULT_URL`, not the catalog's suggested `KEY_VAULT_URI`.

## Add with native Builder CLI

Requires a release listing `app connections` in help; older archives use ARM below.

```bash
builder login
builder app connections list --name "$APP" -s "$SUBSCRIPTION" -g "$RG" --json
builder app connections add --name "$APP" -s "$SUBSCRIPTION" -g "$RG" --connection-json '@.local/connection.json'
builder app connections status --name "$APP" -s "$SUBSCRIPTION" -g "$RG" --connection-name weather
```

The template already granted read-only secret access; saving the connection does not grant it again or retrieve the secret.
`update --connection-json` replaces one complete object; `remove --connection-name weather` removes only pending configuration.
Key Vault is not a protected credential source for Builder: the app SDK reads the provider key.

## Add with ARM instead

Use Bash/`jq` and keep metadata in ignored `.local/`.

```bash
APP_ARM="https://management.azure.com/subscriptions/$SUBSCRIPTION/resourceGroups/$RG/providers/Microsoft.Web/builderApps/$APP"
APP_URL="$APP_ARM?api-version=2026-09-01-preview"
az rest --method get --url "$APP_URL" > .local/app.json
ETAG="$(jq -er .etag .local/app.json)"
jq --slurpfile next .local/connection.json '
  {properties:{versionConfiguration:{connections:
    ((.properties.versionConfiguration.connections // [] |
      map(select((.name|ascii_downcase) != ($next[0].name|ascii_downcase)))) + $next)}}}
' .local/app.json > .local/update.json
az rest --method patch --url "$APP_URL" --headers "If-Match=$ETAG" --body @.local/update.json
az rest --method get --url "$APP_URL" --query properties.versionConfiguration.connections
```

Other connections, manual values, source and scaling are preserved. On 412 refresh/reconcile, never bypass the guard.
For an existing app, remove old manual vault URI/name without deploying before saving the new mappings; retain its original approved expiry.

## Deploy and verify

Use connected GitHub source or `git archive --format=zip --output=../weather-source.zip HEAD` followed by `builder app deploy --name "$APP" -s "$SUBSCRIPTION" -g "$RG" --source-archive ../weather-source.zip --manifest ./builder.yaml --json`.
Wait for terminal success and current active version; health/setup alone is not vault/provider proof.
Verify a bounded real weather lookup with the README's owner-approved credential/window.
An expired 410 is the intended safety boundary, not authorization to renew it.
