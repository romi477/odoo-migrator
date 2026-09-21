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
    """Checkout root. ``rules/`` and ``prompts/`` live here, not inside the package."""
    if getattr(sys, "frozen", False):

        return package_dir()

    return package_dir().parent


def rules_dir() -> Path:

    return repo_dir() / "rules"


def prompts_dir() -> Path:

    return repo_dir() / "prompts"
