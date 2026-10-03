# kv-secret-rotation

Azure Function (Python, v2 model) triggered by Key Vault secret events via Event Grid.
On `SecretNearExpiry` or `SecretExpired`, it works out what kind of secret it is. Storage SAS
tokens are rotated automatically (a new SAS is written back as a new version of the same
secret); every other kind gets a Jira ticket for manual rotation. Rotation for other kinds will
be added one at a time.

Target: `kvrot-sri-func` in `bigwx-rg-kvrot-sri` (Flex Consumption, Python 3.14).

## How rotation works

1. Ignores any event other than `SecretNearExpiry` / `SecretExpired` (stops the
   `SecretNewVersionCreated` loop caused by its own write).
2. Skips it if the latest version is newer than the one in the event (duplicate delivery).
3. Works out the secret type (see below).
4. If the function can rotate that type (today only `storage-sas`), it does, saving the new
   value with the new expiry and the same tags. The new expiry is what makes `SecretNearExpiry`
   fire again next cycle.
5. Raises a Jira ticket either way (see below): after a rotation, so an engineer gets the
   consumers onto the new value; when it can't rotate, so an engineer rotates it manually.

Other errors (throttling, outages, Key Vault access) raise, so Event Grid retries and
eventually dead-letters the event.

## Secret types

The type comes from, in order:

1. a `secret_type` tag, set to one of the types below (overrides the name)
2. the name prefix, following the naming standard
3. the value: a SAS that doesn't follow the naming standard is still `storage-sas`
4. otherwise `unknown`

| Name | Type | Contains | Rotated automatically |
|---|---|---|---|
| `sas-{storage-account}-{container}` | `storage-sas` | SAS token | yes |
| `spn-{app-name}-{purpose}` | `spn` | service principal client secret | not yet |
| `db-{server}-{database}` | `database` | database password | not yet |
| `api-{system}-{purpose}` | `api-key` | API token | not yet |
| `ext-{vendor}-{keyname}` | `external` | vendor or client credential | not yet |
| anything else | `unknown` | can't tell | no |

The prefix needs its hyphen, so `dbpassword` is `unknown`. Types without automatic rotation get
a `manual_rotation_required` ticket whose `action` has the steps for that type.

To add rotation for a type, write a function `(name, value, tags, vault subscription) -> (new value, expiry)` that
raises `ManualRotationRequired` when it can't rotate, and add it to `ROTATORS` in
`function_app.py`.

## Jira tickets

> **Currently disabled.** The three calls that raise tickets are commented out in
> `function_app.py` (marked `Jira disabled while testing rotation`), as are the `JIRA_*` app
> settings in the infra. Uncomment both to turn tickets on. While disabled, "Cannot rotate"
> outcomes only log a warning.

The function calls the Jira automation incoming webhook in `JIRA_WEBHOOK_URL` for every
expiry event it acts on. The `outcome` field says which kind of ticket it is.

| `outcome` | When | Engineer's job |
|---|---|---|
| `rotated` | A new SAS was written | Get the secret's consumers onto the new version before the previous token expires (Key Vault warns 30 days ahead, so the old token has about that long) |
| `manual_rotation_required` | The secret can't be rotated automatically | Rotate it manually, or fix the cause so the next one is automatic |

A secret can't be rotated automatically when:

- the value is not a SAS and the secret has no `storage_account` tag
- the SAS is a kind it doesn't handle (user delegation `skoid`, stored access policy `si`,
  or a resource other than account, container or blob)
- a required detail is missing (container, blob path, subscription)
- the storage account can't be found, or the function has no role on it (403/404)
- the token lifetime is 31 days or less. `SecretNearExpiry` fires 30 days before expiry, so
  a shorter token would be near expiry as soon as it is written and rotate in a loop.

The request is a JSON `POST`. Every ticket has `outcome`, `secretType`, `summary`, `action`, `vaultUrl`,
`secretName`, `secretVersion`, `secretId`, `expiresOn`, `contentType` and `tags`. A `rotated`
ticket adds `previousVersion`; `secretVersion` and `expiresOn` are those of the new version.
A `manual_rotation_required` ticket adds `reason` and `eventType`. The secret value is never
sent. If `JIRA_WEBHOOK_TOKEN` is set, it is sent in the `X-Automation-Webhook-Token` header.

The function tags the version a ticket is about with `rotation_ticket`, so a repeated delivery
raises no second ticket. A version that couldn't be rotated gets the event type, so
`SecretExpired` after `SecretNearExpiry` raises one more. A rotated version also gets
`rotated_from=<previous version>`; if the webhook fails after the rotation, the retry sees the
rotated version has no ticket yet and raises it. Neither tag is copied to the next version.
If the webhook is not configured or the call fails, the function raises so Event Grid retries.

## Where SAS settings come from

Each setting comes from, in order: a tag on the secret, the existing SAS in the secret
value, the secret name (`sas-{storage-account}-{container}`), then the default. So with no tags, the new token is a copy of the old one with new
start and expiry times.

| Setting | Tag | From existing SAS | Default |
|---|---|---|---|
| Storage account | `storage_account` | URL host (`<account>.blob.core.windows.net`), else the name | required |
| Resource group | `storage_rg` | looked up by account name | |
| Subscription | `subscription_id` | | the vault's subscription (from the event), else `AZURE_SUBSCRIPTION_ID` |
| Permissions | `permissions` | `sp` | `rl` |
| Services (account SAS) | `services` | `ss` | `b` |
| Resource types (account SAS) | `resource_types` | `srt` | `c` |
| IP filter | `ip` | `sip` | none |
| Container | `container` | `sr=c` plus URL path, else the name | |
| Blob | | `sr=b` plus URL path | |
| Lifetime | `expiry_days` | `se - st` of the old token | 180 days |
| Protocol | | `spr` | any |
| Encryption scope | | `ses` | none |
| Response headers | | `rscc`, `rscd`, `rsce`, `rscl`, `rsct` | none |

The new value keeps the old shape: a full URL stays a URL with the same path, `?token` keeps
its `?`, and a bare token stays bare.

Storage account names have no hyphens, so in `sas-{storage-account}-{container}` everything
after the second hyphen is the container. The name only fills gaps: it never turns an account
SAS into a container SAS.

If the name disagrees with a `storage_account` or `container` tag, or with the account or
container in the SAS URL, the secret is **not rotated**. One of them is wrong, and rotating could
put a token for the wrong resource into the secret. It logs, for example, `Secret name says
storage account bigwxrgsrib6ac but the storage_account tag says other` and, with Jira on, raises
a manual rotation ticket. Fix whichever is wrong. Comparisons ignore case. Names that don't follow
`sas-…` have nothing to disagree with, so tags and the URL are used as they are.

A value that is not a SAS is never rotated from the name alone, because there is no old token to
copy permissions from; it needs a `storage_account` tag (and then gets the default settings).
A bare token with no account in a tag or the name can't be rotated either, so it raises a Jira ticket, as do user delegation SAS (`skoid`) and stored access policy SAS (`si`).

A lifetime taken from the old token is rounded to whole days, so the 15-minute backdated start
doesn't make each token slightly longer-lived than the last.

```bash
az keyvault secret set-attributes --vault-name <kv> -n <secret> \
  --tags storage_account=<sa> storage_rg=<rg>
```

## Infrastructure and permissions

Everything around the function is in `../kv-secret-rotation-infra` (Terraform): the function
app `kvrot-sri-func` in `bigwx-rg-kvrot-sri`, its user-assigned identity, storage, App
Insights, the roles, and a policy that connects Key Vaults to the function. See its README.

| Function identity needs | Scope | Granted by |
|---|---|---|
| Key Vault Secrets Officer | each vault the policy connects (RBAC-mode vaults) | the policy |
| Access policy `get`, `set` on secrets | each vault the policy connects (access-policy vaults) | the policy |
| `<prefix> SAS signer` (custom: read the account, list keys) | each storage account it signs SAS for | Terraform, `rotation_storage_accounts` |

Add every storage account the function issues SAS tokens for to `rotation_storage_accounts`
in `terraform.tfvars`, with its `subscription_id` if it is not in the subscription deployed into.
The function assumes a storage account is in the same subscription as the vault holding the
secret; give the secret a `subscription_id` tag when it isn't. A SAS for an account that isn't listed raises a manual rotation ticket
(403 listing keys).

App settings (set by Terraform):

| Setting | Purpose |
|---|---|
| `AZURE_CLIENT_ID` | which identity `DefaultAzureCredential` uses |
| `AZURE_SUBSCRIPTION_ID` | fallback subscription for storage accounts, used only when there is no `subscription_id` tag and the event has no vault ID |
| `JIRA_WEBHOOK_URL`, `JIRA_WEBHOOK_TOKEN` | Jira webhook (commented out while Jira is disabled) |

## Deploy

```bash
# one-time: install Core Tools
brew tap azure/functions
brew install azure-functions-core-tools@4

az login
cd utils/kv-secret-rotation
func azure functionapp publish kvrot-sri-func
```

Confirm the function exists:

```bash
az functionapp function list -g bigwx-rg-kvrot-sri -n kvrot-sri-func --query "[].name" -o tsv
```

## How Key Vault reaches the function

The policy (`kvrot-sri-kv-events`, assigned to `bigwx-rg-sri`) gives each vault in scope an
Event Grid subscription `kv-secret-rotation` to the function, for `SecretNearExpiry` and
`SecretExpired` only. It is created on the vault, so it uses the vault's existing system topic
whatever it is called. Check a vault:

```bash
KV_ID=$(az keyvault show -n bigwx-kv-sri --query id -o tsv)
az eventgrid event-subscription list --source-resource-id $KV_ID \
  --query "[].{name:name, state:provisioningState, types:filter.includedEventTypes}" -o table
```

Event Grid calls the function's built-in webhook
(`https://kvrot-sri-func.azurewebsites.net/runtime/webhooks/eventgrid?functionName=kv_secret_expiry`).
The function returning normally counts as delivered (2xx), including "Cannot rotate" and
"Skipping" outcomes; an unhandled error returns 500 and Event Grid retries.

## Retries and dead-lettering

Event Grid retries a failed delivery with backoff (about 10s, 30s, 1m, 5m, 10m, 30m, 1h, 3h,
6h, then every 12h). The subscription allows 30 attempts, but events live for 24 hours
(`eventTimeToLiveInMinutes: 1440`), which runs out after about a dozen attempts. The event then
goes to the `eventgrid-deadletter` container in `bigwxkvrotsri`, as a blob under
`<topic>/<subscription>/<yyyy>/<mm>/<dd>/<hh>/<guid>.json` with `deadLetterReason` and
`lastDeliveryOutcome`:

```bash
az storage blob list --account-name bigwxkvrotsri -c eventgrid-deadletter --auth-mode login \
  --query "[].name" -o tsv
```

Dead-lettering uses the storage account directly (no identity), so the account keeps shared key
access and public network access enabled.

The second function, `deadletter_check`, runs every 15 minutes. For each new blob in the
dead-letter container it logs an error with the event type, secret, version, `deadLetterReason`
and `lastDeliveryOutcome`, then marks the blob with `reported=true` metadata so it is reported
once. That error triggers the `dead-lettered` email alert (see the infra README). It reads the
container from the `DEADLETTER_ACCOUNT_URL` and `DEADLETTER_CONTAINER` app settings.

## Test

`SecretNearExpiry` fires 30 days before expiry, or straight away if the expiry is already less
than 30 days off. Setting an expiry a couple of minutes away triggers it, then `SecretExpired`.

A type the function doesn't rotate (here `api-key`) logs `Cannot rotate` and, with Jira on,
raises a `manual_rotation_required` ticket:

```bash
KV=bigwx-kv-sri
az keyvault secret set --vault-name $KV -n api-test-demo \
  --value dummy --expires $(date -u -v+2M +%Y-%m-%dT%H:%M:%SZ)
```

A SAS is replaced with a new version and, with Jira on, raises a `rotated` ticket. The secret's
`--expires` only controls when the event fires; the new token's lifetime comes from the old
token (`se - st`, here the 180-day default since `generate-sas` sets no start), and must be over
31 days:

```bash
SA=<storage-account>   # must be in rotation_storage_accounts
SAS=$(az storage container generate-sas --account-name $SA -n <container> --permissions rl \
  --expiry $(date -u -v+60d +%Y-%m-%dT%H:%MZ) --auth-mode key -o tsv)
az keyvault secret set --vault-name $KV -n sas-$SA-<container> \
  --value "https://$SA.blob.core.windows.net/<container>?$SAS" \
  --tags storage_rg=<storage-rg> \
  --expires $(date -u -v+2M +%Y-%m-%dT%H:%M:%SZ)
```

### Send an event yourself

The webhook accepts the same request Event Grid sends, so you can trigger a run without waiting
for Key Vault. It runs the deployed code against the real secret, so a SAS really is rotated.
`Version` must be the secret's latest version, or the run is skipped as already rotated.

```bash
APP=kvrot-sri-func; SECRET=<secret-name>
KEY=$(az functionapp keys list -g bigwx-rg-kvrot-sri -n $APP --query systemKeys.eventgrid_extension -o tsv)
VER=$(az keyvault secret show --vault-name $KV -n $SECRET --query id -o tsv | awk -F/ '{print $NF}')

curl -sS -i -X POST "https://$APP.azurewebsites.net/runtime/webhooks/eventgrid?functionName=kv_secret_expiry&code=$KEY" \
  -H "Content-Type: application/json" -H "aeg-event-type: Notification" \
  --data "[{\"id\":\"$(uuidgen)\",\"subject\":\"$SECRET\",\"eventType\":\"Microsoft.KeyVault.SecretNearExpiry\",
    \"eventTime\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"dataVersion\":\"1\",
    \"data\":{\"VaultName\":\"$KV\",\"ObjectType\":\"Secret\",\"ObjectName\":\"$SECRET\",\"Version\":\"$VER\"}}]"
```

`202` means the function returned normally, `500` that it raised. Without the
`aeg-event-type: Notification` header the host treats the request as a subscription handshake.

## Troubleshooting

**Did Event Grid deliver it?** Topic metrics for the last hour: `PublishSuccessCount` (Key
Vault sent an event), `MatchedEventCount` (it passed the event type filter), then
`DeliverySuccessCount` or `DeliveryAttemptFailCount`. Published but not matched means the event
was a type the subscription filters out, such as `SecretNewVersionCreated`.

```bash
TOPIC=$(az eventgrid system-topic list -g bigwx-rg-sri --query "[0].id" -o tsv)
for m in PublishSuccessCount MatchedEventCount DeliverySuccessCount DeliveryAttemptFailCount DeadLetteredCount; do
  echo "== $m"
  az monitor metrics list --resource "$TOPIC" --metrics $m --interval PT1M --offset 1h \
    --aggregation Total --query 'value[0].timeseries[].data[?total > `0`].[timeStamp, total]' -o tsv
done
```

**What did the function do?** App Insights `kvrot-sri-ai` (the portal's Invocations tab and
Log stream lag by several minutes):

```bash
az monitor app-insights query -g bigwx-rg-kvrot-sri -a kvrot-sri-ai --analytics-query '
  union traces, exceptions
  | where timestamp > ago(1h) and operation_Name == "kv_secret_expiry"
  | project timestamp, itemType, severityLevel, message = coalesce(message, outerMessage)
  | order by timestamp asc' --query "tables[0].rows" -o table
```

| Log line | Meaning |
|---|---|
| `event_type=... secret=... version=...` | the event reached the function |
| `Secret ... is type ... (from ...)` | how it classified the secret |
| `Rotating ...` / `Rotated ...: new version ...` | a new SAS was written |
| `Cannot rotate ... (type): reason` (warning) | not rotated; with Jira on, a manual ticket |
| `Skipping ...: already rotated` | a newer version exists (duplicate or later event) |
| exception, `Executed ... (Failed)` | unhandled error; Event Grid retries |

The Azure SDK's own logging is limited to warnings, so these are the only info lines per run.
