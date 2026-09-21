"""The shared key n8n presents to the app. Not Sheller's admin.key — never that one."""

import secrets
from pathlib import Path

API_KEY_HEADER = "X-Odoo-Migrator-Key"

KEY_FILENAME = "api.key"


def load_or_create(path: Path) -> str:
    """Return the key stored at ``path``, creating it on first start.

    Owner-readable only. The value is never printed and never rendered: the
    tool is demonstrated on a projector, and a key in the scrollback is a key
    in the recording. The human copies it out of the file when they configure
    the executor.
    """
    if path.is_file():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:

            return stored
    path.parent.mkdir(parents=True, exist_ok=True)
    minted = secrets.token_urlsafe(32)
    path.write_text(minted + "\n", encoding="utf-8")
    path.chmod(0o600)

    return minted
