"""Refuse a request that did not come from this app's own pages.

Binding 127.0.0.1 keeps other machines out. It does not keep out another
page open in the same browser: any site can submit a form to
`http://127.0.0.1:8000`, and the browser sends it. With no login, one such
form pointed Sheller's base URL at a host of the sender's choosing, and the
next run sent it the code and the write keys. Another name can also be made
to resolve to 127.0.0.1 after its page has loaded (DNS rebinding), which
lets that page read the answers as well.

Two checks close both. Every request names this machine in `Host`: a
rebound name arrives with its own name there. A request that changes
something comes from a page of this origin: the browser says where it came
from — `Sec-Fetch-Site`, else `Origin` — and a page cannot forge either. A
client that sends neither (curl, the suite) is not a browser, and a browser
is what this guards against.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# The names a browser uses for this machine. The port is not checked: a
# rebound name is refused by its name, whatever port it asked for.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# `none` is a request the person started themselves: typed, or a bookmark.
SAME_ORIGIN_SITES = frozenset({"same-origin", "none"})


def host_name(host: str) -> str:
    """`host` without its port, lower case. `[::1]:8000` is `[::1]`."""
    value = host.strip().lower()
    if value.startswith("["):

        return value.split("]", 1)[0] + "]"

    return value.split(":", 1)[0]


def is_loopback_host(host: str) -> bool:

    return host_name(host) in LOOPBACK_NAMES


def from_this_origin(headers: dict[str, str], host: str) -> bool:
    """Whether a browser says this request came from a page of ours.

    `Sec-Fetch-Site` when it is sent, since it is the browser's own verdict.
    Without it, `Origin` has to be this host; `null` is a sandboxed frame or
    a local file, and is not.
    """
    site = headers.get("sec-fetch-site")
    if site is not None:

        return site.strip().lower() in SAME_ORIGIN_SITES
    origin = headers.get("origin")
    if origin is None:

        return True
    parsed = urlsplit(origin.strip())

    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host.strip().lower()


class LocalOnly:
    """ASGI middleware: the two checks above, before any route runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)

            return
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers") or []
        }
        host = headers.get("host", "")
        if not is_loopback_host(host):
            await _refuse(
                scope, send, 400, f"Refused: {host or 'no Host'} is not this machine."
            )

            return
        unsafe = scope["type"] == "http" and scope.get("method", "GET") not in SAFE_METHODS
        if unsafe and not from_this_origin(headers, host):
            await _refuse(scope, send, 403, "Refused: this request came from another site.")

            return
        await self.app(scope, receive, send)


async def _refuse(scope, send, status: int, text: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})

        return
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
