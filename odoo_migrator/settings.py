"""App-wide singleton settings. n8n_base_url lives only here."""

from odoo_migrator.db import database
from odoo_migrator.models import AppSettings

# The settings form never renders the stored key, so it arrives blank on every
# save. This sentinel is how a save says "keep what is there" as opposed to
# "clear it" — clearing has its own endpoint.
KEEP = "__unchanged__"


def _row(settings: AppSettings) -> dict:

    return {
        "n8n_base_url": settings.n8n_base_url,
        "n8n_api_key": settings.n8n_api_key,
        "app_base_url": settings.app_base_url,
        "sheller_base_url": settings.sheller_base_url,
        "sheller_image_tag": settings.sheller_image_tag,
    }


def get_app_settings() -> dict:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            row = AppSettings.create()

        return _row(row)


def clear_n8n_api_key() -> None:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is not None:
            row.n8n_api_key = ""
            row.save()


def save_app_settings(
    *,
    n8n_base_url: str,
    n8n_api_key: str,
    app_base_url: str,
    sheller_base_url: str,
    sheller_image_tag: str,
) -> dict:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            row = AppSettings.create()
        row.n8n_base_url = n8n_base_url.strip()
        if n8n_api_key != KEEP:
            row.n8n_api_key = n8n_api_key.strip()
        row.app_base_url = app_base_url.strip()
        row.sheller_base_url = sheller_base_url.strip()
        row.sheller_image_tag = sheller_image_tag.strip()
        row.save()

        return _row(row)
