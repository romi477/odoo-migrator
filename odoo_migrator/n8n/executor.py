"""POST the trigger at n8n's production webhook. The stub stays the fallback."""


class N8nExecutor:
    def __init__(self, n8n, webhook_path: str = "migrate-module"):
        self.n8n = n8n
        self.webhook_path = webhook_path

    def start(self, payload: dict) -> None:
        path = str(payload.get("webhook_path") or self.webhook_path).strip("/")
        self.n8n.trigger(path, payload)
