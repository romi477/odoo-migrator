"""A daemon's dotted version, read once, and the releases that added routes."""

from __future__ import annotations

from odoo_migrator.sheller.errors import ShellerError

# The first Sheller with `POST /api/sessions/{id}/click_module`.
CLICKBOT_SINCE = (1, 12, 0)


def parse_version(text: str) -> tuple[int, ...]:
    """`1.13.2` as `(1, 13, 2)`. A piece that is not a number is left out."""

    return tuple(int(piece) for piece in str(text or "").split(".") if piece.isdigit())


def daemon_version(sheller) -> tuple[int, ...]:
    """What `GET /health` says, or `()` when the daemon cannot say.

    Not knowing is not an error: a caller that needs a feature tries it and
    reads the route's own answer.
    """
    try:

        return parse_version(sheller.health().get("version") or "")
    except ShellerError:

        return ()
