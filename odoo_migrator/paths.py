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


def user_dir() -> Path:
    """This machine's own directory, beside the database and ``.env``.

    What the app writes that is not part of the build lives here. A build
    directory is replaced by the next upgrade.
    """

    return Path.home() / ".odoo-migrator"


def _checkout(name: str) -> Path | None:
    checkout = repo_dir() / name
    if checkout.is_dir():

        return checkout

    return None


def _data_dir(name: str) -> Path:
    """The checkout's ``name/`` when there is one, else the copy in the package.

    A wheel and a freeze have no checkout. They carry these directories as
    ``odoo_migrator/bundled_<name>`` (see ``pyproject.toml``). Resolving only
    against the checkout used to find nothing in an installed build: no rules,
    no system prompt, no stock cartridge — the first job refused to start.
    """
    checkout = _checkout(name)
    if checkout is not None:

        return checkout
    bundled = package_dir() / f"bundled_{name}"
    if bundled.is_dir():

        return bundled

    return repo_dir() / name


def rules_dir() -> Path:

    return _data_dir("rules")


def prompts_dir() -> Path:

    return _data_dir("prompts")


def cartridges_dir() -> Path:

    return _data_dir("cartridges")


def rules_overlay_dir() -> Path | None:
    """Where a promoted finding goes when there is no checkout to write to.

    In a checkout the rules are the files in git and Promote appends to them
    there, which is the point of keeping them reviewable. An installed build
    reads them from inside the wheel: a directory the next upgrade replaces,
    and often one the process cannot write to at all. Promote would have
    appended a finding to it and lost it without saying so. A file here is
    read in front of the build's own copy.
    """
    if _checkout("rules") is not None:

        return None

    return user_dir() / "rules"
