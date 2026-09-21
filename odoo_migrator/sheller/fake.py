"""In-memory Sheller. Same interface as HttpShellerClient; no daemon required."""

import json
import secrets
import uuid

from odoo_migrator.sheller.errors import ShellerError, ShellerNotOwner

# databases[0] is a different Odoo major's database — the trap that 410s a
# real session. db_name is the one this container is configured for.
CORE = "/opt/odoo/addons"
PROJECT = "/opt/project"
ENTERPRISE = "/opt/enterprise"

# Shaped like a real answer: mostly core, a couple of the project's own, and
# a module depending on more than one other.
DEFAULT_GRAPH = [
    {"name": "base", "depends": [], "state": "installed", "addons_root": CORE,
     "author": "Odoo S.A.", "license": "LGPL-3", "is_core": True},
    {"name": "sale", "depends": ["base"], "state": "installed", "addons_root": CORE,
     "author": "Odoo S.A.", "license": "LGPL-3", "is_core": True},
    {"name": "web", "depends": ["base"], "state": "installed", "addons_root": CORE,
     "author": "odoo", "license": "LGPL-3", "is_core": True},
    # Enterprise: outside the install tree, so only the licence gives it away.
    {"name": "account_reports", "depends": ["sale"], "state": "installed",
     "addons_root": ENTERPRISE, "author": "Odoo S.A.", "license": "OEEL-1",
     "is_core": False},
    # An Enterprise directory really did hold LGPL-3 modules too; this one is
    # kept, because showing one module too many is cheaper than hiding one.
    {"name": "web_enterprise_helper", "depends": ["web"], "state": "installed",
     "addons_root": ENTERPRISE, "author": "Odoo S.A.", "license": "LGPL-3",
     "is_core": False},
    # A customer module that copied its manifest header from a core module —
    # the reason the author field cannot be the filter.
    {"name": "sale_custom", "depends": ["sale", "web"], "state": "installed",
     "addons_root": PROJECT, "author": "Odoo S.A.", "license": "OPL-1",
     "is_core": False},
    {"name": "stock_custom", "depends": ["sale_custom", "web"], "state": "installed",
     "addons_root": PROJECT, "author": "VentorTech", "license": "OPL-1",
     "is_core": False},
]


class FakeShellerClient:
    def __init__(
        self,
        *,
        version: str = "1.7.0",
        container: bool = False,
        mcp: dict | None = None,
    ):
        self._version = version
        self._container = container
        self._mcp = mcp
        self._sessions: dict[str, dict] = {}
        self._containers = [
            {"id": "0123456789ab", "name": "odoo-old", "image": "odoo:17", "status": "Up"},
            {"id": "ba9876543210", "name": "odoo-new", "image": "odoo:18", "status": "Up"},
        ]
        self.graph_modules = [dict(row) for row in DEFAULT_GRAPH]
        self.opens: list[dict] = []
        self.execs: list[str] = []
        self.closes: list[str] = []
        self.commits: list[str] = []
        self.exec_error: Exception | None = None

    def health(self) -> dict:

        return {
            "ok": True,
            "version": self._version,
            "container": self._container,
            "mcp": self._mcp,
        }

    def list_containers(self) -> list:

        return [dict(entry) for entry in self._containers]

    def probe(self, container: str) -> dict:
        if container not in {entry["name"] for entry in self._containers}:
            raise ShellerError(f"unknown container: {container}", status=404)

        return {
            "ok": True,
            "odoo_bin": "/opt/odoo/odoo-bin",
            "odoo_version": "18.0",
            "odoo_major": 18,
            "db_name": "acme_17",
            "databases": ["other_major_db", "acme_17"],
        }

    def open_session(
        self,
        *,
        container: str | None = None,
        database: str | None = None,
        odoo_bin: str | None = None,
        owner: dict | None = None,
        allow_commit: bool | None = None,
        **_extra,
    ) -> dict:
        # The daemon answers 422 here, and the fake must too. A double that
        # accepts what the real thing refuses lets the mistake through and
        # then agrees with the code under test that it was fine.
        if not (container and database and odoo_bin):
            raise ShellerError(
                "container, database and odoo_bin are required", status=422
            )
        session_id = uuid.uuid4().hex
        write_key = secrets.token_hex(16)
        self._sessions[session_id] = {
            "id": session_id,
            "state": "ready",
            "container": container,
            "database": database,
            "odoo_bin": odoo_bin,
            "owner": dict(owner or {}),
            "allow_commit": bool(allow_commit),
            "write_key": write_key,
        }
        described = self.get_session(session_id)
        described["write_key"] = write_key
        self.opens.append(
            {
                "id": session_id,
                "container": container,
                "database": database,
                "odoo_bin": odoo_bin,
                "owner": dict(owner or {}),
                "allow_commit": bool(allow_commit),
            }
        )

        return described

    def list_sessions(self) -> list:

        return [self.get_session(session_id) for session_id in self._sessions]

    def get_session(self, session_id: str) -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            raise ShellerError("unknown session", status=404)

        return {key: value for key, value in session.items() if key != "write_key"}

    def exec(self, session_id: str, code: str, *, write_key: str) -> dict:
        self._require(session_id, write_key)
        if self.exec_error is not None:
            raise self.exec_error
        self.execs.append(code)
        if "ir.module.module" in code:
            payload = json.dumps(self.graph_modules)

            return {"ok": True, "result": repr(payload), "error": None}

        return {"ok": True, "result": "None", "code": code, "error": None}

    def run_test(
        self,
        session_id: str,
        test: str,
        *,
        write_key: str,
        timeout: float | None = None,
    ) -> dict:
        self._require(session_id, write_key)

        return {"ok": True, "test": test}

    def commit(self, session_id: str, *, write_key: str) -> dict:
        session = self._require(session_id, write_key)
        self.commits.append(session_id)
        if not session["allow_commit"]:
            raise ShellerError("commit_not_allowed", status=423)

        return {"ok": True}

    def rollback(self, session_id: str, *, write_key: str) -> dict:
        self._require(session_id, write_key)

        return {"ok": True}

    def close(self, session_id: str, *, write_key: str) -> dict:
        self._require(session_id, write_key)
        self.closes.append(session_id)
        del self._sessions[session_id]

        return {"ok": True}

    def _require(self, session_id: str, write_key: str) -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            raise ShellerError("unknown session", status=404)
        # Measured against the daemon on 8765: a close with an empty key is
        # 403 not_owner, for an agent session we had just opened ourselves.
        # There is no corpse exemption to lean on either — a session whose
        # process dies is announced and drops out of the registry, so it is
        # never in a listing to be closed.
        if write_key != session["write_key"]:
            raise ShellerNotOwner("not_owner", status=403)

        return session
