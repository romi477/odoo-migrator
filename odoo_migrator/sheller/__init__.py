"""Sheller HTTP client and its in-memory fake. The app attaches to the daemon
at the URL in Settings; it does not start one."""

from odoo_migrator.sheller.client import (
    SESSION_KEY_HEADER,
    HttpShellerClient,
    ShellerClient,
)
from odoo_migrator.sheller.errors import (
    ShellerBusy,
    ShellerError,
    ShellerNeedsHuman,
    ShellerNotOwner,
    ShellerSessionDead,
    ShellerTimeout,
    ShellerUnreachable,
)
from odoo_migrator.sheller.fake import FakeShellerClient

__all__ = [
    "SESSION_KEY_HEADER",
    "FakeShellerClient",
    "HttpShellerClient",
    "ShellerBusy",
    "ShellerClient",
    "ShellerError",
    "ShellerNeedsHuman",
    "ShellerNotOwner",
    "ShellerSessionDead",
    "ShellerTimeout",
    "ShellerUnreachable",
]
