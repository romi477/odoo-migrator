"""App-wide singleton settings."""

from odoo_migrator.db import database
from odoo_migrator.models import AppSettings


def _row(settings: AppSettings) -> dict:

    return {
        "sheller_base_url": settings.sheller_base_url,
        "sheller_image_tag": settings.sheller_image_tag,
    }


def get_app_settings() -> dict:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            row = AppSettings.create()

        return _row(row)


def save_app_settings(*, sheller_base_url: str, sheller_image_tag: str) -> dict:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            row = AppSettings.create()
        row.sheller_base_url = sheller_base_url.strip()
        row.sheller_image_tag = sheller_image_tag.strip()
        row.save()

        return _row(row)
