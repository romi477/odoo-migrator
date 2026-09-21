"""Create and activate the stock cartridge in a running n8n.

Called by packaging/n8n.sh. Publishing is not one API call: every
sub-workflow an Execute Workflow node points at has to be active before the
parent, or 2.39.9 answers 400 and the production webhook stays 404.
"""

import os
import sys

from odoo_migrator.n8n import HttpN8nClient, N8nError, publish_stock


def main() -> int:
    base_url = os.environ.get("N8N_BASE_URL", "http://127.0.0.1:5678")
    api_key = os.environ.get("N8N_API_KEY", "")
    if not api_key:
        print(
            "N8N_API_KEY is unset. n8n mints it in its own UI "
            "(Settings > n8n API), after the owner account exists.",
            file=sys.stderr,
        )

        return 2
    client = HttpN8nClient(base_url, api_key=api_key)
    try:
        published = publish_stock(client)
    except N8nError as refused:
        print(f"n8n refused: {refused}", file=sys.stderr)

        return 1
    for name, workflow_id in sorted(published["ids"].items()):
        print(f"{name:26s} {workflow_id}")
    print()
    print(f"production webhook: {base_url}/webhook/{published['webhook_path']}")
    print("Set that base URL in the app's settings, then pin the cartridge")
    print("on the migration and smoke-run one module.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
