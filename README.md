# kv-secret-rotation

Azure Function (Python, v2 model) triggered by Key Vault secret events via Event Grid.
On `SecretNearExpiry` or `SecretExpired`, it generates a new storage SAS token and writes it
back as a new version of the same secret.

Target: `sri-test` in `bigwx-rg-sri` (Flex Consumption, Python 3.14).

## How rotation works

1. Ignores any event other than `SecretNearExpiry` / `SecretExpired` (stops the
   `SecretNewVersionCreated` loop caused by its own write).
2. Reads the secret. Skips it if it has no `storage_account` tag.
3. Skips it if the latest version is newer than the one in the event (duplicate delivery).
4. Gets key1 of the storage account, signs a new SAS, and saves it with the new expiry and
   the same tags. The new expiry is what makes `SecretNearExpiry` fire again next cycle.

Any error raises, so Event Grid retries and eventually dead-letters the event.

## Secret tags

| Tag | Required | Default | Notes |
|---|---|---|---|
| `storage_account` | yes | | Storage account name |
| `storage_rg` | yes | | Storage account resource group |
| `subscription_id` | no | `AZURE_SUBSCRIPTION_ID` app setting | |
| `permissions` | no | `rl` | SAS permissions, e.g. `racwdl` |
| `services` | no | `b` | Account SAS only: `b`, `f`, `q`, `t` |
| `resource_types` | no | `c` | Account SAS only: `s`, `c`, `o` |
| `expiry_days` | no | `120` | Lifetime of each new token |
| `container` | no | | Set to issue a container SAS instead of an account SAS |
| `ip` | no | | `1.2.3.4` or `1.2.3.4-1.2.3.10` |

```bash
az keyvault secret set-attributes --vault-name <kv> -n <secret> \
  --tags storage_account=<sa> storage_rg=<rg> permissions=rl expiry_days=120
```

The token is stored without a leading `?`.

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

az functionapp config appsettings set -g bigwx-rg-sri -n sri-test \
  --settings AZURE_SUBSCRIPTION_ID=<subscription-id>
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

Create a secret that expires soon. `SecretNearExpiry` fires 30 days before expiry, so the
fastest test is `SecretExpired`:

```bash
az keyvault secret set --vault-name $KV -n test-sas \
  --value dummy --expires $(date -u -v+2M +%Y-%m-%dT%H:%M:%SZ)
```

Watch the logs (allow a few minutes after expiry):

```bash
func azure functionapp logstream sri-test
```

or in App Insights: `traces | where message has "hello"`.
