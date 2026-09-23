"""App-wide singleton settings."""

from odoo_migrator.db import database
from odoo_migrator.models import AppSettings


def _row(settings: AppSettings) -> dict:

    return {
        "sheller_base_url": settings.sheller_base_url,
        "sheller_image_tag": settings.sheller_image_tag,
        "anthropic_api_key_set": bool(settings.anthropic_api_key),
    }


def get_app_settings() -> dict:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            row = AppSettings.create()

        return _row(row)


def save_app_settings(
    *,
    sheller_base_url: str,
    sheller_image_tag: str,
    anthropic_api_key: str | None = None,
) -> dict:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            row = AppSettings.create()
        row.sheller_base_url = sheller_base_url.strip()
        row.sheller_image_tag = sheller_image_tag.strip()
        if anthropic_api_key:
            row.anthropic_api_key = anthropic_api_key.strip()
        row.save()

        return _row(row)


def anthropic_api_key() -> str:
    """The stored key. Callers that talk to the model read it here."""
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            return ""

        return row.anthropic_api_key
