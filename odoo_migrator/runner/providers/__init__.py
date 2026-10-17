"""Providers, by name. The runner asks ``get`` and does not branch."""

from __future__ import annotations

from odoo_migrator.runner.providers.anthropic import AnthropicProvider
from odoo_migrator.runner.providers.base import Provider, SettingField
from odoo_migrator.runner.providers.cursor import CursorProvider
from odoo_migrator.runner.providers.local import LocalProvider
from odoo_migrator.runner.providers.openai import OpenAIProvider
from odoo_migrator.runner.providers.scripted import ScriptedProvider

REGISTRY: dict[str, Provider] = {}


def register(provider: Provider) -> None:
    REGISTRY[provider.name] = provider


def get(name: str) -> Provider:
    try:

        return REGISTRY[name]
    except KeyError as exc:
        raise KeyError(name) from exc


def known_models() -> dict[str, frozenset[str]]:

    return {name: provider.models for name, provider in REGISTRY.items()}


def model_allowed(name: str) -> bool:

    return any(provider.accepts_model(name) for provider in REGISTRY.values())


# The order is the settings page's and the provider list's on a card.
register(AnthropicProvider())
register(OpenAIProvider())
register(CursorProvider())
register(LocalProvider())
register(ScriptedProvider(client=None))

__all__ = [
    "REGISTRY",
    "CursorProvider",
    "LocalProvider",
    "OpenAIProvider",
    "Provider",
    "ScriptedProvider",
    "SettingField",
    "get",
    "known_models",
    "model_allowed",
    "register",
]
