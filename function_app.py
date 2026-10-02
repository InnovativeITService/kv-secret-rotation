import logging

import azure.functions as func

app = func.FunctionApp()


@app.event_grid_trigger(arg_name="event")
def kv_secret_expiry(event: func.EventGridEvent):
    data = event.get_json()

    # logging.info("hello")
    logging.info(
        "event_type=%s subject=%s vault=%s secret=%s version=%s exp=%s",
        event.event_type,
        event.subject,
        data.get("VaultName"),
        data.get("ObjectName"),
        data.get("Version"),
        data.get("EXP"),
    )
