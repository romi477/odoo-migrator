"""Run the app: odoo-migrator [--reload], or python -m odoo_migrator."""

import argparse
from pathlib import Path

import uvicorn


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


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    host = "127.0.0.1"

    # flush: with stdout on a pipe — a packaging script, a log file, the
    # desktop app later — these sit in the buffer and surface after
    # uvicorn's banner, or not at all if the process is killed.
    print(f"ui: http://{host}:{args.port}/", flush=True)

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
