"""App-wide singleton settings."""

from odoo_migrator.db import database
from odoo_migrator.models import AppSettings

# Shown in a password field when a secret is stored. It is not the secret:
# bullets, then the last few characters so two keys can be told apart. Every
# key of one provider starts the same way, so the tail is what differs. A
# key too short to give up a tail without giving up much is all bullets.
# A save that posts a mask back leaves the stored value alone.
BULLET = "\u2022"
SECRET_MASK = BULLET * 8
SECRET_TAIL = 6
SECRET_TAIL_MIN_LENGTH = 16


def mask_secret(value: str) -> str:
    """What a field shows in place of a stored secret."""
    if len(value) < SECRET_TAIL_MIN_LENGTH:

        return SECRET_MASK

    return SECRET_MASK + value[-SECRET_TAIL:]


def _row(settings: AppSettings) -> dict:
    from odoo_migrator.runner.providers import REGISTRY

    fields = []
    for provider in REGISTRY.values():
        for field in provider.settings:
            stored = provider_setting(provider.name, field.key)
            fields.append(
                {
                    "provider": provider.name,
                    "key": field.key,
                    "label": field.label,
                    "secret": field.secret,
                    "input_name": f"provider_{provider.name}_{field.key}",
                    "set": bool(stored),
                    "mask": mask_secret(stored),
                    # A URL is not a secret: shown as written, so a typo in
                    # it can be seen. A key is never rendered.
                    "value": "" if field.secret else stored,
                }
            )

    return {
        "sheller_base_url": settings.sheller_base_url,
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
    anthropic_api_key: str | None = None,
    provider_values: dict[str, str] | None = None,
    cleared: set[str] | None = None,
) -> dict:
    with database.connection_context():
        row = AppSettings.select().first()
        if row is None:
            row = AppSettings.create()
        # Before anything is written: a refused save leaves every field as it
        # was, not half of them.
        _refuse_a_damaged_mask(provider_values or {})
        row.sheller_base_url = sheller_base_url.strip()
        if anthropic_api_key:
            _store_setting("anthropic", "api_key", anthropic_api_key.strip())
        gone = cleared or set()
        for name in gone:
            provider, _, key = name.partition("__")
            if provider and key:
                _clear_setting(provider, key)
        for name, value in (provider_values or {}).items():
            if name in gone:
                continue
            provider, _, key = name.partition("__")
            text = value.strip()
            # The mask the page showed is the field left alone. Anything else
            # that starts with a bullet was refused above.
            if provider and key and text and not text.startswith(BULLET):
                _store_setting(provider, key, text)
        row.save()

        return _row(row)


def _refuse_a_damaged_mask(provider_values: dict[str, str]) -> None:
    """A field that still shows its mask with something typed after it.

    Pasting a key at the end of the mask posts bullets, the old tail and the
    new key. No key holds a bullet, so it was never taken for one, and it was
    not the mask either: the old key stayed, silently, and the person went on
    believing the new one was saved.
    """
    from odoo_migrator.contract import ContractError

    for name, value in provider_values.items():
        text = value.strip()
        provider, _, key = name.partition("__")
        if not (provider and key and text.startswith(BULLET)):
            continue
        if text != mask_secret(provider_setting(provider, key)):
            raise ContractError(
                "A key field holds the saved key's mask with more typed after "
                "it. Nothing was saved: clear the field, paste the whole key "
                "and save again."
            )


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


def _clear_setting(provider: str, key: str) -> None:
    from odoo_migrator.models import ProviderSetting

    existing = ProviderSetting.get_or_none(
        (ProviderSetting.provider == provider) & (ProviderSetting.key == key)
    )
    if existing is not None:
        existing.delete_instance()
    if provider == "anthropic" and key == "api_key":
        row = AppSettings.select().first()
        if row is not None:
            row.anthropic_api_key = ""
            row.save()


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
