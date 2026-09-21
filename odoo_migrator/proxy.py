"""Thin HTTP pass-through to the Sheller daemon. Admin routes stay unproxied."""

import posixpath

from odoo_migrator.sheller.client import SESSION_KEY_HEADER

ADMIN_HEADER = "x-os-admin-key"


class AdminKeyForbidden(Exception):
    """The proxy never forwards X-OS-Admin-Key and never reads admin.key."""


class PathNotAllowed(Exception):
    """The path is outside what the proxy is willing to forward."""


def normalized_path(path: str) -> str:
    """Collapse a path the way the upstream HTTP client will.

    ``GET /api/sessions/%2e%2e/journals`` arrives here decoded but not
    collapsed, so a prefix allowlist reads it as a sessions call — and then
    httpx normalises it to ``/api/journals`` on the way out and the daemon
    serves an unmasked journal. Collapse first, allow second, and forward the
    collapsed path so the three never disagree.
    """
    collapsed = posixpath.normpath(path)
    if not collapsed.startswith("/"):
        raise PathNotAllowed(path)

    return collapsed


def allowed_path(path: str) -> bool:
    if ".." in path.split("/"):

        return False
    if path == "/health":

        return True
    if path.startswith("/api/journals"):

        return False

    return path.startswith(("/api/sessions", "/api/containers", "/api/probe"))


def outgoing_headers(incoming: dict[str, str]) -> dict[str, str]:
    lowered = {key.lower(): value for key, value in incoming.items()}
    if ADMIN_HEADER in lowered:
        raise AdminKeyForbidden()
    forwarded: dict[str, str] = {}
    content_type = lowered.get("content-type")
    if content_type:
        forwarded["content-type"] = content_type
    session_key = lowered.get(SESSION_KEY_HEADER.lower())
    if session_key is not None:
        forwarded[SESSION_KEY_HEADER] = session_key

    return forwarded
