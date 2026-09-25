"""Run the app: odoo-migrator [--reload], or python -m odoo_migrator."""

import argparse
import os
from pathlib import Path

import uvicorn

from odoo_migrator.db import default_env_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="odoo-migrator", description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--reload",
        action="store_true",
        help=(
            "restart on Python changes (development only). Files under web/ "
            "are ignored — reload the browser page instead."
        ),
    )

    return parser


class _Server(uvicorn.Server):
    """Wake the SSE streams the moment the signal arrives.

    uvicorn waits for in-flight requests before it runs lifespan shutdown,
    and an SSE stream is in-flight for its whole window — so closing the
    hub from lifespan is too late to help, and SIGTERM looked like a hang
    that ended in SIGKILL. handle_exit runs first.
    """

    def handle_exit(self, sig, frame):
        hub = getattr(getattr(self.config.app, "state", None), "hub", None)
        if hub is not None:
            hub.close()
        super().handle_exit(sig, frame)


def load_env_file(path: Path) -> None:
    """Set variables that the shell has not already set.

    A line is ``KEY=value``, optional ``export``, optional quotes. Comments
    and blank lines are skipped. An existing environment value wins, so a
    one-off prefix still overrides the file.
    """
    if not path.is_file():

        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ[key] = value


def main() -> None:
    load_env_file(default_env_path())
    parser = build_parser()
    args = parser.parse_args()
    host = "127.0.0.1"
    runner = "native" if os.environ.get("ODOO_MIGRATOR_RUNNER") == "native" else "stub"

    # flush: with stdout on a pipe — a packaging script, a log file, the
    # desktop app later — these sit in the buffer and surface after
    # uvicorn's banner, or not at all if the process is killed.
    print(f"ui: http://{host}:{args.port}/", flush=True)
    print(f"runner: {runner}", flush=True)

    if args.reload:
        uvicorn.run(
            "odoo_migrator.api:create_app",
            factory=True,
            host=host,
            port=args.port,
            reload=True,
            reload_dirs=[str(Path(__file__).parent)],
            reload_excludes=["web/*"],
        )

        return

    from odoo_migrator.api import create_app

    app = create_app()
    server = _Server(
        uvicorn.Config(
            app,
            host=host,
            port=args.port,
            # A backstop. Nothing should reach it now that the streams are
            # woken on the signal, but a hung request must not mean SIGKILL.
            timeout_graceful_shutdown=10,
        )
    )
    server.run()


if __name__ == "__main__":
    main()
