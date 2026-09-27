"""Locate package data whether we are a checkout or a frozen binary."""

import sys
from pathlib import Path


def package_dir() -> Path:
    """Directory that holds ``web/``.

    This module is bytecode inside the PYZ in a freeze, so ``Path(__file__)``
    is not a real file next to the templates. PyInstaller puts data under
    ``sys._MEIPASS``.
    """
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        bundled = meipass / "odoo_migrator"
        if (bundled / "web").is_dir():

            return bundled

        return meipass

    return Path(__file__).resolve().parent


def web_dir() -> Path:

    return package_dir() / "web"


def templates_dir() -> Path:
    """Jinja sources. Deliberately outside the mounted static tree: a mount
    over the whole of ``web/`` served template source at ``/static/*.html``.
    """

    return web_dir() / "templates"


def static_dir() -> Path:

    return web_dir() / "static"


def repo_dir() -> Path:
    """Checkout root. ``rules/``, ``prompts/`` and ``cartridges/`` live here."""
    if getattr(sys, "frozen", False):

        return package_dir()

    return package_dir().parent


def _data_dir(name: str) -> Path:
    """The checkout's ``name/`` when there is one, else the copy in the package.

    A wheel and a freeze have no checkout. They carry these directories as
    ``odoo_migrator/bundled_<name>`` (see ``pyproject.toml``). Resolving only
    against the checkout used to find nothing in an installed build: no rules,
    no system prompt, no stock cartridge — the first job refused to start.
    """
    checkout = repo_dir() / name
    if checkout.is_dir():

        return checkout
    bundled = package_dir() / f"bundled_{name}"
    if bundled.is_dir():

        return bundled

    return checkout


def rules_dir() -> Path:

    return _data_dir("rules")


def prompts_dir() -> Path:

    return _data_dir("prompts")


def cartridges_dir() -> Path:

    return _data_dir("cartridges")
