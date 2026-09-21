"""Run the app: odoo-migrator [--reload], or python -m odoo_migrator."""

import argparse
from pathlib import Path

import uvicorn

from odoo_migrator.authkey import KEY_FILENAME
from odoo_migrator.db import default_db_path

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="odoo-migrator", description=__doc__)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "bind address. Loopback by default. A containerised executor "
            "cannot reach loopback, so serving n8n means widening this "
            "deliberately (0.0.0.0 on Docker Desktop, the bridge gateway "
            "on Linux)"
        ),
    )
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


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    print(f"ui: http://{args.host}:{args.port}/")
    # Path, never the value: this tool is demonstrated on a projector.
    print(f"api key: {default_db_path().parent / KEY_FILENAME}")
    if args.host not in LOOPBACK:
        # 0.0.0.0 is every interface, not "the container gateway". Behind the
        # File API and the Sheller proxy sit host file writes and Python as
        # SUPERUSER_ID, and the pages themselves ask nobody for anything.
        print(
            f"warning: {args.host} serves every network this machine is on. "
            f"The API key in {default_db_path().parent / KEY_FILENAME} is the "
            "only thing in front of the File API and the Sheller proxy, and "
            "the pages have nothing in front of them at all."
        )

    if args.reload:
        uvicorn.run(
            "odoo_migrator.api:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[str(Path(__file__).parent)],
            reload_excludes=["web/*"],
        )

        return

    from odoo_migrator.api import create_app

    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
