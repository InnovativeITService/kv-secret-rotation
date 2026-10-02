# kv-secret-rotation

Azure Function (Python, v2 model) triggered by Key Vault secret events via Event Grid.
Currently logs `hello` plus the secret details. SAS rotation logic goes in `kv_secret_expiry`.

Target: `sri-test` in `bigwx-rg-sri` (Flex Consumption, Python 3.14).

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
