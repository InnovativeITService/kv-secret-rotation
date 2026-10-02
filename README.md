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

To add rotation for a type, write a function `(name, value, tags) -> (new value, expiry)` that
raises `ManualRotationRequired` when it can't rotate, and add it to `ROTATORS` in
`function_app.py`.

## Jira tickets

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
| Subscription | `subscription_id` | | `AZURE_SUBSCRIPTION_ID` app setting |
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
SAS into a container SAS, and a name that disagrees with the SAS URL is logged and ignored.

A value that is not a SAS is never rotated from the name alone, because there is no old token to
copy permissions from; it needs a `storage_account` tag (and then gets the default settings).
A bare token with no account in a tag or the name can't be rotated either, so it raises a Jira ticket, as do user delegation SAS (`skoid`) and stored access policy SAS (`si`).

A lifetime taken from the old token is rounded to whole days, so the 15-minute backdated start
doesn't make each token slightly longer-lived than the last.

```bash
az keyvault secret set-attributes --vault-name <kv> -n <secret> \
  --tags storage_account=<sa> storage_rg=<rg>
```

## Permissions

```bash
PID=$(az functionapp identity assign -g bigwx-rg-sri -n sri-test --query principalId -o tsv)

# vault using access policies (bigwx-kv-sri)
az keyvault set-policy -n <kv> --object-id $PID --secret-permissions get set

# vault using Azure RBAC
az role assignment create --assignee-object-id $PID --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets Officer" --scope $(az keyvault show -n <kv> --query id -o tsv)

# repeat for every storage account it issues tokens for
az role assignment create --assignee-object-id $PID --assignee-principal-type ServicePrincipal \
  --role "Storage Account Key Operator Service Role" --scope <storage-account-id>

# only needed when secrets have no storage_rg tag, so the resource group can be looked up
az role assignment create --assignee-object-id $PID --assignee-principal-type ServicePrincipal \
  --role "Reader" --scope <storage-account-id>

az functionapp config appsettings set -g bigwx-rg-sri -n sri-test \
  --settings AZURE_SUBSCRIPTION_ID=<subscription-id> \
    JIRA_WEBHOOK_URL=<webhook-url> JIRA_WEBHOOK_TOKEN=<webhook-token>
```

Check which model a vault uses with
`az keyvault show -n <kv> --query properties.enableRbacAuthorization`.

## Deploy

```bash
# one-time: install Core Tools
brew tap azure/functions
brew install azure-functions-core-tools@4

az login
cd utils/kv-secret-rotation
func azure functionapp publish sri-test
```

Confirm the function exists:

```bash
az functionapp function list -g bigwx-rg-sri -n sri-test --query "[].name" -o tsv
```

## Wire Key Vault to the function

```bash
RG=bigwx-rg-sri
KV=<your-key-vault-name>
KV_RG=<key-vault-resource-group>

KV_ID=$(az keyvault show -n $KV -g $KV_RG --query id -o tsv)
FN_ID=$(az functionapp show -g $RG -n sri-test --query id -o tsv)/functions/kv_secret_expiry

az eventgrid event-subscription create \
  --name kv-secret-expiry-to-sri-test \
  --source-resource-id "$KV_ID" \
  --endpoint-type azurefunction \
  --endpoint "$FN_ID" \
  --included-event-types Microsoft.KeyVault.SecretNearExpiry Microsoft.KeyVault.SecretExpired
```

The Microsoft.EventGrid resource provider must be registered on the subscription.

## Dead-lettering

If the function keeps failing, Event Grid retries delivery (default: 30 attempts or 24h, whichever comes
first). Events that still fail after that go to the `eventgrid-deadletter` container in `bigwxrgsrib6ac`.

```bash
SA=bigwxrgsrib6ac
SA_ID=$(az storage account show -g $RG -n $SA --query id -o tsv)
TOPIC=$(az eventgrid system-topic list -g $RG --query "[?contains(source,'$KV')].name" -o tsv)

az storage container-rm create -g $RG --storage-account $SA -n eventgrid-deadletter --public-access off

az eventgrid system-topic event-subscription update -g $RG --system-topic-name $TOPIC \
  -n kv-secret-expiry-to-sri-test \
  --deadletter-endpoint "$SA_ID/blobServices/default/containers/eventgrid-deadletter"
```

Dead-lettered events land as blobs under
`<topic>/<subscription>/<yyyy>/<mm>/<dd>/<hh>/<guid>.json`, and each event includes
`deadLetterReason` and `lastDeliveryOutcome`:

```bash
az storage blob list --account-name $SA -c eventgrid-deadletter --auth-mode login --query "[].name" -o tsv
```

Note: identity-based dead-lettering (`deadLetterWithResourceIdentity`) did not persist on this
system topic subscription when set through the REST API, so this uses the plain
`--deadletter-endpoint`. That needs the storage account to allow public network access.

## Test

`SecretNearExpiry` fires 30 days before expiry, so the fastest test is `SecretExpired` on a
secret that expires in a couple of minutes.

A value the function can't rotate raises a `manual_rotation_required` ticket:

```bash
az keyvault secret set --vault-name $KV -n test-manual \
  --value dummy --expires $(date -u -v+2M +%Y-%m-%dT%H:%M:%SZ)
```

A SAS it can rotate is replaced with a new version and raises a `rotated` ticket. The secret's
`--expires` only controls when the event fires; the new token's lifetime comes from the old
token (`se - st`, here 180 days by default since `generate-sas` sets no start), and must be
over 31 days:

```bash
SAS=$(az storage container generate-sas --account-name $SA -n <container> --permissions rl \
  --expiry $(date -u -v+60d +%Y-%m-%dT%H:%MZ) --auth-mode key -o tsv)
az keyvault secret set --vault-name $KV -n test-sas \
  --value "https://$SA.blob.core.windows.net/<container>?$SAS" \
  --tags storage_rg=<storage-rg> \
  --expires $(date -u -v+2M +%Y-%m-%dT%H:%M:%SZ)
```

Watch the logs (allow a few minutes after expiry):

```bash
func azure functionapp logstream sri-test
```

or in App Insights: `traces | where message has "Rotat" or message has "ticket"`.
