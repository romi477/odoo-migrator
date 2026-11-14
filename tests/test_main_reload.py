"""--reload runs the same server as a plain start: the streams wake on the signal."""

import sys
from types import SimpleNamespace

import uvicorn
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

import odoo_migrator.__main__ as entry


class _Hub:
    closed = False

    def close(self):
        self.closed = True


def _app(hub):
    async def app(scope, receive, send):  # pragma: no cover - never served
        pass

    app.state = SimpleNamespace(hub=hub)

    return app


def test_the_signal_closes_the_hub_of_an_app_loaded_by_name():
    """With --reload the config holds an import string, and the app the
    worker serves is `loaded_app`, wrapped in uvicorn's middleware."""
    hub = _Hub()
    config = uvicorn.Config("odoo_migrator.api:create_app", factory=True)
    config.loaded_app = ProxyHeadersMiddleware(_app(hub))
    server = entry._Server(config)

    server.handle_exit(15, None)

    assert hub.closed and server.should_exit


def test_reload_runs_the_worker_on_the_server_that_wakes_the_streams(monkeypatch):
    seen = {}

    class Supervisor:
        def __init__(self, config, target, sockets):
            seen["config"], seen["target"] = config, target

        def run(self):
            seen["ran"] = True

    monkeypatch.setattr(entry, "ChangeReload", Supervisor)
    monkeypatch.setattr(uvicorn.Config, "bind_socket", lambda self: object())
    monkeypatch.setattr(entry, "load_env_file", lambda path: None)
    monkeypatch.setattr(sys, "argv", ["odoo-migrator", "--reload", "--port", "8123"])

    entry.main()

    assert seen["ran"]
    assert isinstance(seen["target"].__self__, entry._Server)
    config = seen["config"]
    assert config.host == "127.0.0.1" and config.port == 8123
    assert config.should_reload and config.timeout_graceful_shutdown == 10
