import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, unquote, urlsplit

import azure.functions as func
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.storage import StorageManagementClient
from azure.storage.blob import generate_account_sas, generate_blob_sas, generate_container_sas

app = func.FunctionApp()

ROTATE_EVENT_TYPES = {
    "Microsoft.KeyVault.SecretNearExpiry",
    "Microsoft.KeyVault.SecretExpired",
}

# Secret types, from the name prefix of the naming standard. A secret_type tag (one of the
# values, or "unknown") overrides the name. A secret matching neither whose value is a SAS
# is treated as storage-sas.
#   sas-{storage-account}-{container}   storage-sas  SAS token
#   spn-{app-name}-{purpose}            spn          service principal client secret
#   db-{server}-{database}              database     database password
#   api-{system}-{purpose}              api-key      API token
#   ext-{vendor}-{keyname}              external     vendor or client credential
#   anything else                       unknown
NAME_PREFIXES = {
    "sas": "storage-sas",
    "spn": "spn",
    "db": "database",
    "api": "api-key",
    "ext": "external",
}
SECRET_TYPES = set(NAME_PREFIXES.values()) | {"unknown"}

# What the engineer does when a type can't be rotated automatically
MANUAL_STEPS = {
    "storage-sas": "Issue a new SAS for the same account and scope, save it as a new version of this secret, "
    "then fix the cause below so the next rotation is automatic.",
    "spn": "Add a new client secret to the app registration, save it as a new version of this secret, "
    "move the consumers to it, then delete the old client secret.",
    "database": "Set a new password for the database login, save it as a new version of this secret, "
    "and update the consumers.",
    "api-key": "Generate a new key in the issuing system, save it as a new version of this secret, "
    "move the consumers to it, then revoke the old key.",
    "external": "Request a new credential from the vendor or client, save it as a new version of this secret, "
    "and confirm when the old one is retired.",
    "unknown": "Work out what this secret is and rotate it. Rename it to the naming standard or add a "
    "secret_type tag so future rotations are routed correctly.",
}

# SAS settings are taken from, in order: a tag on the secret, the existing SAS in the secret
# value, the secret name (sas-{storage-account}-{container}), then DEFAULTS. Tags:
#   storage_account  storage account name (else from the SAS URL host, else the name)
#   storage_rg       storage account resource group (else looked up by name)
#   subscription_id  subscription of the storage account (else AZURE_SUBSCRIPTION_ID app setting)
#   permissions      SAS permissions (sp)
#   services         account SAS services (ss)
#   resource_types   account SAS resource types (srt)
#   container        issue a container SAS for this container (else from sr=c and the SAS URL path,
#                    else the name when the value is not a SAS)
#   ip               allowed IP or range (sip)
#   expiry_days      lifetime of the new token (else the old token's se - st)
DEFAULTS = {
    "permissions": "rl",
    "services": "b",
    "resource_types": "c",
    "expiry_days": "180",
}

# Response header overrides carried over from container and blob SAS tokens
RESPONSE_HEADER_PARAMS = {
    "rscc": "cache_control",
    "rscd": "content_disposition",
    "rsce": "content_encoding",
    "rscl": "content_language",
    "rsct": "content_type",
}

# Backdate the start time so clients with slightly skewed clocks can use the token immediately
CLOCK_SKEW = timedelta(minutes=15)

# Key Vault raises SecretNearExpiry this long before expiry. A token that lives no longer
# than this (plus a margin) would be near expiry as soon as it is written and rotate again.
NEAR_EXPIRY_WINDOW = timedelta(days=30)
MIN_LIFETIME = NEAR_EXPIRY_WINDOW + timedelta(days=1)

# Set on a secret version once a ticket has been raised for it, so a duplicate delivery raises
# no second ticket. It holds TICKET_ROTATED on a rotated version, or the event type on a version
# that couldn't be rotated, so SecretExpired after SecretNearExpiry raises one more.
TICKET_TAG = "rotation_ticket"
TICKET_ROTATED = "rotated"

# Set on a rotated version, holding the version it replaced. Lets a retry raise the ticket for
# a rotation whose ticket failed, instead of skipping the secret as already rotated.
ROTATED_FROM_TAG = "rotated_from"

# App settings: the Jira automation incoming webhook URL and, optionally, the value sent in
# its X-Automation-Webhook-Token header
JIRA_WEBHOOK_URL = "JIRA_WEBHOOK_URL"
JIRA_WEBHOOK_TOKEN = "JIRA_WEBHOOK_TOKEN"

credential = DefaultAzureCredential()


class ManualRotationRequired(Exception):
    """The secret can't be rotated automatically; a person needs to look at it."""


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
    secret_type, type_source = classify(secret_name, tags, current.value)

    # Event Grid can deliver the same event more than once. If the latest version
    # is no longer the one that is expiring, it has already been rotated.
    if event_version and current.properties.version != event_version:
        # Jira disabled while testing rotation; uncomment to raise tickets again
        # if tags.get(ROTATED_FROM_TAG) == event_version and TICKET_TAG not in tags:
        #     logging.info("Rotated %s earlier but its ticket was not raised; raising it now", secret_name)
        #     ticket_rotated(secrets, current.properties, secret_type)
        #     return
        logging.info(
            "Skipping %s: already rotated (event version %s, latest %s)",
            secret_name,
            event_version,
            current.properties.version,
        )
        return

    logging.info("Secret %s is type %s (from %s)", secret_name, secret_type, type_source)
    rotate = ROTATORS.get(secret_type)
    try:
        if rotate is None:
            raise ManualRotationRequired(f"Automatic rotation is not available for {secret_type} secrets")
        new_value, expiry = rotate(secret_name, current.value, tags)
    except ManualRotationRequired as exc:
        logging.warning("Cannot rotate %s (%s): %s", secret_name, secret_type, exc)
        # Jira disabled while testing rotation; uncomment to raise tickets again
        # ticket_manual_rotation(secrets, current.properties, secret_type, event.event_type, str(exc))
        return

    new = secrets.set_secret(
        secret_name,
        new_value,
        expires_on=expiry,
        tags={**own_tags_removed(tags), ROTATED_FROM_TAG: current.properties.version},
        content_type=current.properties.content_type,
    )
    logging.info("Rotated %s: new version %s expires %s", secret_name, new.properties.version, expiry.isoformat())
    # Jira disabled while testing rotation; uncomment to raise tickets again
    # ticket_rotated(secrets, new.properties, secret_type)


def classify(name: str, tags: dict, value: Optional[str]) -> tuple:
    """Return the secret's type and where it came from (tag, name or value)."""
    tagged = (tags.get("secret_type") or "").strip().lower()
    if tagged in SECRET_TYPES:
        return tagged, "secret_type tag"
    if tagged:
        logging.warning("Ignoring unrecognised secret_type tag %r on %s", tagged, name)

    prefix, sep, _ = name.lower().partition("-")
    if sep and prefix in NAME_PREFIXES:
        return NAME_PREFIXES[prefix], "name"
    if parse_sas(value):
        return "storage-sas", "value"
    return "unknown", "name and value"


def rotate_storage_sas(name: str, value: Optional[str], tags: dict) -> tuple:
    """Issue a new SAS with the same settings as the old one. Returns (new value, expiry)."""
    existing = parse_sas(value)
    # Without an old token there is nothing to copy permissions from; only issue one from
    # scratch (with DEFAULTS) when a storage_account tag asks for it explicitly
    if existing is None and "storage_account" not in tags:
        raise ManualRotationRequired("Value is not a SAS and the secret has no storage_account tag")
    cfg = resolve_settings(tags, existing, sas_name_parts(name))
    logging.info(
        "Rotating %s: account=%s kind=%s container=%s blob=%s sp=%s ss=%s srt=%s sip=%s spr=%s lifetime=%s",
        name,
        cfg["account"],
        cfg["kind"],
        cfg["container"],
        cfg["blob"],
        cfg["permissions"],
        cfg["services"],
        cfg["resource_types"],
        cfg["ip"],
        cfg["protocol"],
        cfg["lifetime"],
    )
    token, expiry = build_sas(cfg)
    return format_value(token, existing), expiry


# Secret types the function can rotate. Every other type gets a manual rotation ticket.
ROTATORS = {
    "storage-sas": rotate_storage_sas,
}


def sas_name_parts(name: str) -> dict:
    """Account and container from a sas-{storage-account}-{container} name. Account names have no
    hyphens, so everything after the second hyphen is the container (which may contain hyphens)."""
    parts = name.lower().split("-", 2)
    if len(parts) < 2 or parts[0] != "sas":
        return {}
    return {"account": parts[1] or None, "container": parts[2] if len(parts) == 3 else None}


def parse_sas(value: Optional[str]) -> Optional[dict]:
    """Parse a SAS URL (https://acct.blob.core.windows.net/container/blob?sv=...) or a bare token
    (sv=... or ?sv=...). Returns None if the value is not a SAS."""
    value = (value or "").strip()
    url = urlsplit(value) if value.lower().startswith(("https://", "http://")) else None
    query = url.query if url else value.lstrip("?")
    params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
    if "sig" not in params:
        return None

    info = {
        "params": params,
        "account": None,
        "container": None,
        "blob": None,
        "url_base": None,
        "leading_q": value.startswith("?"),
    }
    if url and url.hostname:
        info["account"] = url.hostname.split(".")[0]
        container, _, blob = url.path.lstrip("/").partition("/")
        info["container"] = unquote(container) or None
        info["blob"] = unquote(blob) or None
        info["url_base"] = f"{url.scheme}://{url.netloc}{url.path}"
    return info


def resolve_settings(tags: dict, existing: Optional[dict], from_name: dict) -> dict:
    params = existing["params"] if existing else {}

    if params.get("skoid"):
        raise ManualRotationRequired("Existing SAS is a user delegation SAS; only account-key SAS tokens are supported")
    if params.get("si"):
        raise ManualRotationRequired("Existing SAS uses a stored access policy (si); rotate the policy, not the token")

    def pick(tag: str, param: Optional[str] = None):
        return tags.get(tag) or (params.get(param) if param else None) or DEFAULTS.get(tag)

    account = tags.get("storage_account") or (existing or {}).get("account") or from_name.get("account")
    if not account:
        raise ManualRotationRequired(
            "No storage account: no storage_account tag, the value is not a SAS URL, "
            "and the name is not sas-{storage-account}-{container}"
        )
    if from_name.get("account") and account.lower() != from_name["account"]:
        logging.warning("Secret name says account %s but the secret is for %s; using %s", from_name["account"], account, account)

    container = tags.get("container") or (existing or {}).get("container") or from_name.get("container")
    blob = (existing or {}).get("blob")
    sr = params.get("sr")
    # The name's container only decides the kind when there is no existing SAS to copy, so an
    # account SAS named sas-{account}-{container} stays an account SAS
    if tags.get("container") or sr == "c" or (not params and from_name.get("container")):
        kind = "container"
    elif sr == "b":
        kind = "blob"
    elif "ss" in params or "srt" in params or not params:
        kind = "account"
    else:
        raise ManualRotationRequired(f"Unsupported SAS resource sr={sr}")
    if kind in ("container", "blob") and not container:
        raise ManualRotationRequired(f"{kind} SAS needs a container (container tag, SAS URL path or secret name)")
    if kind == "blob" and not blob:
        raise ManualRotationRequired("Blob SAS needs the blob path from the SAS URL")

    lifetime = token_lifetime(tags, params)
    if lifetime < MIN_LIFETIME:
        raise ManualRotationRequired(
            f"Token lifetime {lifetime} is shorter than {MIN_LIFETIME.days} days, so the new token would be "
            f"near expiry as soon as it is written; set an expiry_days tag of at least {MIN_LIFETIME.days}"
        )

    subscription_id = tags.get("subscription_id") or os.environ.get("AZURE_SUBSCRIPTION_ID")
    if not subscription_id:
        raise ManualRotationRequired("No subscription_id tag and AZURE_SUBSCRIPTION_ID app setting is not set")

    return {
        "account": account,
        "resource_group": tags.get("storage_rg"),
        "subscription_id": subscription_id,
        "kind": kind,
        "container": container,
        "blob": blob,
        "permissions": pick("permissions", "sp"),
        "services": pick("services", "ss"),
        "resource_types": pick("resource_types", "srt"),
        "ip": pick("ip", "sip"),
        "protocol": params.get("spr"),
        "encryption_scope": params.get("ses"),
        "response_headers": {kw: params[p] for p, kw in RESPONSE_HEADER_PARAMS.items() if params.get(p)},
        "lifetime": lifetime,
    }


def token_lifetime(tags: dict, params: dict) -> timedelta:
    if tags.get("expiry_days"):
        try:
            return timedelta(days=int(tags["expiry_days"]))
        except ValueError:
            raise ManualRotationRequired(f"expiry_days tag {tags['expiry_days']!r} is not a whole number of days")
    try:
        lifetime = parse_time(params["se"]) - parse_time(params["st"])
        if lifetime > timedelta(0):
            # Whole days, so the backdated start of each token doesn't add CLOCK_SKEW every rotation
            return timedelta(days=round(lifetime / timedelta(days=1)))
    except (KeyError, ValueError):
        pass
    return timedelta(days=int(DEFAULTS["expiry_days"]))


def parse_time(value: str) -> datetime:
    # SAS times are UTC ISO 8601: 2026-01-01, 2026-01-01T00:00Z or 2026-01-01T00:00:00Z
    value = value.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised SAS time {value}")


def build_sas(cfg: dict) -> tuple:
    resource_group = cfg["resource_group"] or find_resource_group(cfg["subscription_id"], cfg["account"])
    account_key = get_account_key(cfg["subscription_id"], resource_group, cfg["account"])

    now = datetime.now(timezone.utc)
    common = {
        "account_name": cfg["account"],
        "account_key": account_key,
        "permission": cfg["permissions"],
        "start": now - CLOCK_SKEW,
        "expiry": now + cfg["lifetime"],
        "ip": cfg["ip"],
        "protocol": cfg["protocol"],
        "encryption_scope": cfg["encryption_scope"],
    }

    if cfg["kind"] == "container":
        token = generate_container_sas(container_name=cfg["container"], **common, **cfg["response_headers"])
    elif cfg["kind"] == "blob":
        token = generate_blob_sas(
            container_name=cfg["container"], blob_name=cfg["blob"], **common, **cfg["response_headers"]
        )
    else:
        token = generate_account_sas(resource_types=cfg["resource_types"], services=cfg["services"], **common)

    return token, common["expiry"]


def format_value(token: str, existing: Optional[dict]) -> str:
    """Store the new token in the same shape as the old value: full URL, ?token or bare token."""
    if existing and existing["url_base"]:
        return f"{existing['url_base']}?{token}"
    if existing and existing["leading_q"]:
        return f"?{token}"
    return token


def find_resource_group(subscription_id: str, account_name: str) -> str:
    storage = StorageManagementClient(credential, subscription_id)
    for account in storage.storage_accounts.list():
        if account.name.lower() == account_name.lower():
            return account.id.split("/")[4]
    raise ManualRotationRequired(
        f"Storage account {account_name} not found in subscription {subscription_id}, "
        "or the function has no Reader role on it; add a storage_rg tag or grant the role"
    )


def get_account_key(subscription_id: str, resource_group: str, account_name: str) -> str:
    storage = StorageManagementClient(credential, subscription_id)
    try:
        keys = storage.storage_accounts.list_keys(resource_group, account_name)
    except HttpResponseError as exc:
        # Missing account or role won't fix itself on retry; anything else (throttling, outage) might
        if exc.status_code not in (403, 404):
            raise
        raise ManualRotationRequired(
            f"Cannot list keys of storage account {account_name} in {resource_group} ({exc.status_code}); "
            "check the account exists and the function has Storage Account Key Operator Service Role on it"
        ) from exc
    # azure-mgmt-storage 25+ models are mappings, so `keys` is the dict method; the list is keys_property
    return keys.keys_property[0].value


def own_tags_removed(tags: dict) -> dict:
    """The secret's tags without the ones this function sets, which belong to one version only."""
    return {k: v for k, v in tags.items() if k not in (TICKET_TAG, ROTATED_FROM_TAG)}


def ticket_rotated(secrets: SecretClient, props, secret_type: str) -> None:
    """Ask an engineer to get the consumers of a rotated secret onto the new value."""
    open_ticket(
        secrets,
        props,
        TICKET_ROTATED,
        {
            "outcome": "rotated",
            "secretType": secret_type,
            "summary": f"Update consumers of rotated {secret_type} secret {props.name} in {props.vault_url}",
            "action": "The secret was rotated automatically. Work with the users of this secret so they "
            "pick up the new version before the previous one expires.",
            "previousVersion": (props.tags or {}).get(ROTATED_FROM_TAG),
        },
    )


def ticket_manual_rotation(secrets: SecretClient, props, secret_type: str, event_type: str, reason: str) -> None:
    """Ask an engineer to rotate a secret the function couldn't rotate."""
    open_ticket(
        secrets,
        props,
        event_type,
        {
            "outcome": "manual_rotation_required",
            "secretType": secret_type,
            "summary": f"Rotate {secret_type} secret {props.name} in {props.vault_url} manually",
            "action": MANUAL_STEPS[secret_type],
            "reason": reason,
            "eventType": event_type,
        },
    )


def open_ticket(secrets: SecretClient, props, marker: str, details: dict) -> None:
    """Raise a Jira ticket about a secret version, once per version and marker."""
    tags = props.tags or {}
    if tags.get(TICKET_TAG) == marker:
        logging.info("Ticket already raised for %s version %s (%s)", props.name, props.version, marker)
        return

    post_jira_webhook(
        {
            **details,
            "vaultUrl": props.vault_url,
            "secretName": props.name,
            "secretVersion": props.version,
            "secretId": props.id,
            "expiresOn": props.expires_on.isoformat() if props.expires_on else None,
            "contentType": props.content_type,
            "tags": own_tags_removed(tags),
        }
    )

    # Record the ticket on this version only; the next version starts without the tag
    secrets.update_secret_properties(props.name, props.version, tags={**tags, TICKET_TAG: marker})
    logging.info("Raised %s ticket for %s version %s", details["outcome"], props.name, props.version)


def post_jira_webhook(payload: dict) -> None:
    url = os.environ.get(JIRA_WEBHOOK_URL)
    if not url:
        # Fail so Event Grid retries and then dead-letters the event, rather than dropping it
        raise RuntimeError(f"{JIRA_WEBHOOK_URL} app setting is not set; cannot raise a ticket")

    headers = {"Content-Type": "application/json"}
    if os.environ.get(JIRA_WEBHOOK_TOKEN):
        headers["X-Automation-Webhook-Token"] = os.environ[JIRA_WEBHOOK_TOKEN]

    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    # Raises on a non-2xx response, so Event Grid retries the event
    with urllib.request.urlopen(request, timeout=30) as response:
        logging.info("Jira webhook returned %s", response.status)
