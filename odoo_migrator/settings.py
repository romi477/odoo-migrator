"""App-wide singleton settings."""

from odoo_migrator.db import database
from odoo_migrator.models import AppSettings


def _row(settings: AppSettings) -> dict:
    from odoo_migrator.runner.providers import REGISTRY

    fields = []
    for provider in REGISTRY.values():
        for field in provider.settings:
            fields.append(
                {
                    "provider": provider.name,
                    "key": field.key,
                    "label": field.label,
                    "secret": field.secret,
                    "input_name": f"provider_{provider.name}_{field.key}",
                    "set": bool(provider_setting(provider.name, field.key)),
                }
            )

    return {
        "sheller_base_url": settings.sheller_base_url,
        "sheller_image_tag": settings.sheller_image_tag,
        "anthropic_api_key_set": bool(provider_setting("anthropic", "api_key") or settings.anthropic_api_key),
        "provider_fields": fields,
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
    provider_values: dict[str, str] | None = None,
) -> dict:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            row = AppSettings.create()
        row.sheller_base_url = sheller_base_url.strip()
        row.sheller_image_tag = sheller_image_tag.strip()
        if anthropic_api_key:
            _store_setting("anthropic", "api_key", anthropic_api_key.strip())
        for name, value in (provider_values or {}).items():
            provider, _, key = name.partition("__")
            if provider and key and value.strip():
                _store_setting(provider, key, value.strip())
        row.save()

        return _row(row)


def _store_setting(provider: str, key: str, value: str) -> None:
    from odoo_migrator.models import ProviderSetting

    existing = ProviderSetting.get_or_none(
        (ProviderSetting.provider == provider) & (ProviderSetting.key == key)
    )
    if existing is None:
        ProviderSetting.create(provider=provider, key=key, value=value)

        return
    existing.value = value
    existing.save()


def provider_setting(provider: str, key: str) -> str:
    """One stored value. Empty when the provider has never been given it."""
    from odoo_migrator.models import ProviderSetting

    with database.connection_context():
        row = ProviderSetting.get_or_none(
            (ProviderSetting.provider == provider) & (ProviderSetting.key == key)
        )
        if row is not None and row.value:

            return row.value
        if provider == "anthropic" and key == "api_key":
            legacy = AppSettings.select().first()

            return legacy.anthropic_api_key if legacy is not None else ""

        return ""


def anthropic_api_key() -> str:
    """The stored key. Callers that talk to the model read it here."""
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            return ""

        return row.anthropic_api_key
