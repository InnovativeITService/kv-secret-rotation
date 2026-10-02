import logging
import os
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.storage import StorageManagementClient
from azure.storage.blob import generate_account_sas, generate_container_sas

app = func.FunctionApp()

ROTATE_EVENT_TYPES = {
    "Microsoft.KeyVault.SecretNearExpiry",
    "Microsoft.KeyVault.SecretExpired",
}

# Tags on the Key Vault secret that drive rotation. A secret without
# storage_account is not a SAS secret and is ignored.
#   storage_account  (required) storage account name
#   storage_rg       (required) storage account resource group
#   subscription_id  (optional) defaults to the AZURE_SUBSCRIPTION_ID app setting
#   permissions      (optional) SAS permissions, default "rl"
#   services         (optional) account SAS services, default "b"
#   resource_types   (optional) account SAS resource types, default "c"
#   expiry_days      (optional) lifetime of the new token, default 120
#   container        (optional) issue a container SAS for this container instead of an account SAS
#   ip               (optional) allowed IP or range, e.g. 1.2.3.4 or 1.2.3.4-1.2.3.10
DEFAULTS = {
    "permissions": "rl",
    "services": "b",
    "resource_types": "c",
    "expiry_days": "180",
}

# Backdate the start time so clients with slightly skewed clocks can use the token immediately
CLOCK_SKEW = timedelta(minutes=15)

credential = DefaultAzureCredential()


@app.event_grid_trigger(arg_name="event")
def kv_secret_expiry(event: func.EventGridEvent):
    data = event.get_json()
    vault_name = data.get("VaultName")
    secret_name = data.get("ObjectName")
    event_version = data.get("Version")

    logging.info(
        "event_type=%s vault=%s secret=%s version=%s exp=%s",
        event.event_type,
        vault_name,
        secret_name,
        event_version,
        data.get("EXP"),
    )

    # Writing the new token fires SecretNewVersionCreated; ignoring it here
    # prevents a rotation loop even if the subscription filter includes it.
    if event.event_type not in ROTATE_EVENT_TYPES:
        logging.info("Skipping %s: not an expiry event", event.event_type)
        return

    secrets = SecretClient(vault_url=f"https://{vault_name}.vault.azure.net", credential=credential)
    current = secrets.get_secret(secret_name)
    tags = current.properties.tags or {}

    if "storage_account" not in tags:
        logging.info("Skipping %s: no storage_account tag", secret_name)
        return

    # Event Grid can deliver the same event more than once. If the latest version
    # is no longer the one that is expiring, it has already been rotated.
    if event_version and current.properties.version != event_version:
        logging.info(
            "Skipping %s: already rotated (event version %s, latest %s)",
            secret_name,
            event_version,
            current.properties.version,
        )
        return

    sas_token, expiry = build_sas(tags)

    new = secrets.set_secret(
        secret_name,
        sas_token,
        expires_on=expiry,
        tags=tags,
        content_type=current.properties.content_type,
    )
    logging.info("Rotated %s: new version %s expires %s", secret_name, new.properties.version, expiry.isoformat())


def build_sas(tags: dict) -> tuple:
    cfg = {**DEFAULTS, **tags}

    for required in ("storage_account", "storage_rg"):
        if not cfg.get(required):
            raise ValueError(f"Secret is missing required tag '{required}'")

    subscription_id = cfg.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID")
    if not subscription_id:
        raise ValueError("No subscription_id tag and AZURE_SUBSCRIPTION_ID app setting is not set")

    account_name = cfg["storage_account"]
    account_key = get_account_key(subscription_id, cfg["storage_rg"], account_name)

    now = datetime.now(timezone.utc)
    start = now - CLOCK_SKEW
    expiry = now + timedelta(days=int(cfg["expiry_days"]))
    ip = cfg.get("ip") or None

    if cfg.get("container"):
        token = generate_container_sas(
            account_name=account_name,
            container_name=cfg["container"],
            account_key=account_key,
            permission=cfg["permissions"],
            start=start,
            expiry=expiry,
            ip=ip,
        )
    else:
        token = generate_account_sas(
            account_name=account_name,
            account_key=account_key,
            resource_types=cfg["resource_types"],
            permission=cfg["permissions"],
            services=cfg["services"],
            start=start,
            expiry=expiry,
            ip=ip,
        )

    return token, expiry


def get_account_key(subscription_id: str, resource_group: str, account_name: str) -> str:
    storage = StorageManagementClient(credential, subscription_id)
    keys = storage.storage_accounts.list_keys(resource_group, account_name)
    return keys.keys[0].value
