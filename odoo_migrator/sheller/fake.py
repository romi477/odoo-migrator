"""In-memory Sheller. Same interface as HttpShellerClient; no daemon required."""

import json
import re
import secrets
import uuid

from odoo_migrator.sheller.errors import (
    ShellerBusy,
    ShellerError,
    ShellerNotOwner,
    ShellerSessionDead,
    ShellerTimeout,
)

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


# The daemon's own rule for a test spec: `module.TestClass`, with an
# optional `.test_method`. Copied rather than approximated, because a
# double that is looser than the daemon is how a bad spec reached
# production in the first place.
TEST_SPEC_RE = re.compile(r"^(\w+)\.(\w+)(?:\.(\w+))?$")

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
        self._catalogues: dict[str, list] = {}
        self.tests_run: list[dict] = []
        # The journal file outlives the session. close drops the live row and
        # leaves this, which is what history and the export still read.
        self._journals: dict[str, list] = {}
        self._containers = [
            {"id": "0123456789ab", "name": "odoo-old", "image": "odoo:17", "status": "Up"},
            {"id": "ba9876543210", "name": "odoo-new", "image": "odoo:18", "status": "Up"},
        ]
        self.graph_modules = [dict(row) for row in DEFAULT_GRAPH]
        # container name -> modules installed in its database. What the
        # target's `ir.module.module` answers; empty is a fresh database.
        self.installed_by_container: dict[str, list[str]] = {}
        # container name -> `odoo.addons.__path__` there. Unset is a stock
        # layout: Odoo's own trees and the project.
        self.addons_path_by_container: dict[str, list[str]] = {}
        self.opens: list[dict] = []
        self.execs: list[str] = []
        self._next_command = 0
        self.closes: list[str] = []
        self.commits: list[str] = []
        self.interrupts: list[str] = []
        self.exec_error: Exception | None = None
        # Code that "runs for ever": exec raises ShellerTimeout for it, the
        # way the real client does when the HTTP call expires, and the
        # command stays busy in the session until interrupted.
        self.hangs_on: str | None = None
        # How many `get_session` polls a hung command takes to finish on its
        # own. None is for ever: only an interrupt ends it.
        self.hangs_for: int | None = None
        # fragment -> fields merged into the answer of an exec whose code
        # contains it. How a test makes Odoo raise, or log, for one command.
        self.exec_answers: dict[str, dict] = {}
        # spec -> fields merged into that run's `test` block.
        self.test_outcomes: dict[str, dict] = {}
        self.rollbacks: list[str] = []

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
            "kind": "docker",
            "host": None,
            "stage": None,
            "pending_commands": 0,
            "odoo_bin": odoo_bin,
            "owner": dict(owner or {}),
            "allow_commit": bool(allow_commit),
            "write_key": write_key,
            "commands": [],
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

    # What the daemon's `Session.describe()` actually answers. `odoo_bin` is
    # NOT in it: the daemon takes it when a session opens and never gives it
    # back. The fake used to return it anyway, and `reopen` was written
    # against the fake — it would have raised KeyError on the first real
    # container.
    _DESCRIBED = (
        "id",
        "state",
        "container",
        "database",
        "kind",
        "host",
        "stage",
        "owner",
        "allow_commit",
        "pending_commands",
    )

    def get_session(self, session_id: str) -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            raise ShellerError("unknown session", status=404)
        if self._running(session) and self.hangs_for is not None:
            session["hung_polls"] = session.get("hung_polls", 0) + 1
            if session["hung_polls"] >= self.hangs_for:
                for command in session["commands"]:
                    if command.get("status") == "running":
                        command["status"] = "done"
        if session["state"] in ("ready", "busy"):
            session["state"] = "busy" if self._running(session) else "ready"

        return {key: value for key, value in session.items() if key in self._DESCRIBED}

    def exec(
        self, session_id: str, code: str, *, write_key: str, timeout: float | None = None
    ) -> dict:
        """The daemon's answer: 200 with `error` set when the Python raised.

        This used to answer `{"ok": True}` for any code at all, so a tool
        that sent a bare dotted name as Python passed here and raised
        NameError on every live container.
        """
        session = self._require(session_id, write_key)
        if self.exec_error is not None:
            raise self.exec_error
        self.execs.append(code)
        if self.hangs_on is not None and self.hangs_on in code:
            session["commands"].append(
                {"kind": "exec", "id": self._next_command + 1, "code": code, "status": "running"}
            )
            self._next_command += 1
            session["hung_polls"] = 0

            raise ShellerTimeout(f"no answer within the timeout for {session_id}")
        self._next_command += 1
        command_id = self._next_command
        session["commands"].append(
            {"kind": "exec", "id": command_id, "code": code, "status": "done"}
        )
        session["pending_commands"] += 1
        result = "None"
        if "_os_read()" in code:
            result = repr(json.dumps({"text": "def action_confirm(self):\n    ...", "line": 1}))
        elif "ir.module.module" in code and "json.dumps(rows)" in code:
            result = repr(json.dumps(self.graph_modules))
        elif "ir.module.module" in code and "installed_names" in code:
            names = self.installed_by_container.get(session["container"], [])
            addons_path = self.addons_path_by_container.get(
                session["container"],
                ["/opt/odoo/odoo/addons", "/opt/odoo/addons", PROJECT],
            )
            result = repr(
                json.dumps({"installed": sorted(names), "addons_path": addons_path})
            )
        answer = {
            "id": command_id,
            "stdout": "",
            "stdout_truncated": False,
            "result": result,
            "result_truncated": False,
            "error": None,
            "duration": 0.0,
            "stderr": [],
            "stderr_truncated": False,
        }
        for fragment, fields in self.exec_answers.items():
            if fragment in code:
                answer.update(fields)

        return answer

    def run_test(
        self,
        session_id: str,
        test: str,
        *,
        write_key: str,
        timeout: float | None = None,
    ) -> dict:
        """Refuse a malformed spec, and answer a run the way the daemon does.

        The spec rule is `module.TestClass` or `module.TestClass.test_method`
        — a looser double let `module.Class.{'name': ...}` through. The
        answer is the daemon's too: a red run is `error: None` with
        `test.success` false. It used to be `{"ok": True}`, and the step that
        read it counted every failing test as a pass.
        """
        session = self._require(session_id, write_key)
        if not TEST_SPEC_RE.match(test or ""):
            raise ShellerError(
                "invalid_test_spec",
                status=400,
                detail={
                    "error": "invalid_test_spec",
                    "test": test,
                    "recovery": "use 'module.TestClass' or 'module.TestClass.test_method'",
                },
            )
        self.tests_run.append({"test": test, "session_id": session_id, "timeout": timeout})
        # Odoo's runner rolls back whatever the session held before testing.
        discarded = session["pending_commands"] > 0
        session["pending_commands"] = 0
        module, test_class, test_method = TEST_SPEC_RE.match(test).groups()
        outcome = {
            "module": module,
            "test_class": test_class,
            "test_method": test_method,
            "tests_run": 1,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "success": True,
        }
        if test_method is None:
            outcome.update(self._class_outcome(module, test))
        outcome.update(self.test_outcomes.get(test, {}))
        stderr = list(outcome.pop("stderr", []))

        return {
            "stdout": "",
            "stdout_truncated": False,
            "result": None,
            "result_truncated": False,
            "error": outcome.pop("error", None),
            "duration": 0.0,
            "test": outcome,
            "stderr": stderr,
            "stderr_truncated": False,
            "discarded_pending": discarded,
        }

    def _class_outcome(self, module: str, class_spec: str) -> dict:
        """A class runs every method in it, as the daemon's runner does: the
        count is theirs, and one red method makes the class red."""
        methods = []
        for item in self._catalogues.get(module, []):
            if item.get("spec") == class_spec:
                methods = [method["spec"] for method in item.get("methods") or []]
        if not methods:

            return {}
        merged = {"tests_run": 0, "failures": 0, "errors": 0, "success": True, "stderr": []}
        for spec in methods:
            one = self.test_outcomes.get(spec, {})
            merged["tests_run"] += one.get("tests_run", 1)
            merged["failures"] += one.get("failures", 0)
            merged["errors"] += one.get("errors", 0)
            merged["success"] = merged["success"] and one.get("success", True)
            merged["stderr"].extend(one.get("stderr", []))

        return merged

    def kill(self, session_id: str) -> None:
        """The process died: every call answers 410 until it is closed."""
        self._sessions[session_id]["state"] = "dead"

    def commit(self, session_id: str, *, write_key: str) -> dict:
        session = self._require(session_id, write_key)
        self.commits.append(session_id)
        if not session["allow_commit"]:
            raise ShellerError("commit_not_allowed", status=423)

        return {"ok": True}

    def rollback(self, session_id: str, *, write_key: str) -> dict:
        session = self._require(session_id, write_key)
        self.rollbacks.append(session_id)
        session["pending_commands"] = 0

        return {"ok": True}

    def close(self, session_id: str, *, write_key: str) -> dict:
        session = self._require(session_id, write_key, verb="close")
        self.closes.append(session_id)
        self._journals[session_id] = session
        del self._sessions[session_id]

        return {"ok": True}

    def source(self, session_id: str, *, write_key: str, **target) -> dict:
        """The client's own path: the snippet runs through `exec`."""
        from odoo_migrator.sheller.source import read_source

        return read_source(self, session_id, write_key=write_key, **target)

    def forget_journal(self, session_id: str) -> None:
        """The journal file is gone. The trace row still has the ids."""
        self._sessions.pop(session_id, None)
        if isinstance(self._journals, dict):
            self._journals.pop(session_id, None)

    def journal(self, session_id: str) -> dict:
        session = self._remembered(session_id)

        return {"records": list(session["commands"])}

    def feed(
        self, session_id: str, *, logs: bool = False, log_tail: int | None = None
    ) -> dict:
        session = self._remembered(session_id)
        entries = list(session["commands"])
        body = {
            "history": [entry["code"] for entry in entries if "code" in entry],
            "entries": entries,
            "session": {
                "id": session_id,
                "state": "ready" if session_id in self._sessions else "gone",
            },
        }
        if logs:
            kept = session.get("logs", [])
            if log_tail:
                kept = kept[-log_tail:]
            body["logs"] = kept
            body["logs_truncated"] = bool(log_tail) and len(session.get("logs", [])) > log_tail

        return body

    def tests_for(self, module: str, classes: list) -> None:
        """Seed the catalogue, in the shape the daemon returns.

        A class is ``{"name", "spec", "methods": [{"name", "spec"}]}``. The
        runner used to rebuild specs from names and only ever saw the empty
        catalogue in tests, so it shipped `str(dict)` into a spec and every
        module that had tests failed with 400 `invalid_test_spec`. A double
        that accepts a looser shape hides that again.
        """
        for item in classes:
            if not isinstance(item, dict) or not item.get("spec"):
                raise ShellerError(
                    "a test class is {'name', 'spec', 'methods'}, as the "
                    f"daemon returns it, not {item!r}",
                    status=500,
                )
            for method in item.get("methods") or []:
                if not isinstance(method, dict) or not method.get("spec"):
                    raise ShellerError(
                        "a test method is {'name', 'spec'}, as the daemon "
                        f"returns it, not {method!r}",
                        status=500,
                    )
        self._catalogues[module] = [dict(item) for item in classes]

    @staticmethod
    def catalogue(module: str, classes: dict[str, list[str]]) -> list:
        """Build what the daemon would return for these class/method names."""

        return [
            {
                "name": class_name,
                "spec": f"{module}.{class_name}",
                "methods": [
                    {"name": method, "spec": f"{module}.{class_name}.{method}"}
                    for method in methods
                ],
            }
            for class_name, methods in classes.items()
        ]

    def list_tests(self, session_id: str, *, module: str, write_key: str) -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            raise ShellerError("unknown session", status=404)
        # The catalogue route does not check a session key. The argument stays
        # so every session verb has the same shape.
        del write_key

        return {
            "module": module,
            "path": f"/opt/project/{module}",
            "classes": list(self._catalogues.get(module, [])),
            "container": session["container"],
        }

    def interrupt(self, session_id: str, *, write_key: str) -> dict:
        self._require(session_id, write_key, verb="interrupt")
        self.interrupts.append(session_id)
        for command in self._sessions[session_id]["commands"]:
            if command.get("status") == "running":
                command["status"] = "interrupted"

        return {"ok": True}

    @staticmethod
    def _running(session: dict) -> bool:

        return any(command.get("status") == "running" for command in session["commands"])

    def _remembered(self, session_id: str) -> dict:
        session = self._sessions.get(session_id) or self._journals.get(session_id)
        if session is None:
            raise ShellerError("unknown session", status=404)

        return session

    def _require(self, session_id: str, write_key: str, *, verb: str = "exec") -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            raise ShellerError("unknown session", status=404)
        if session["state"] == "dead" and verb != "close":
            raise ShellerSessionDead(
                "session_gone: the shell process exited", status=410
            )
        if verb == "exec" and self._running(session):
            raise ShellerBusy("session_busy: a command is still running", status=409)
        # Measured against the daemon on 8765: a close with an empty key is
        # 403 not_owner, for an agent session we had just opened ourselves.
        # There is no corpse exemption to lean on either — a session whose
        # process dies is announced and drops out of the registry, so it is
        # never in a listing to be closed.
        if write_key != session["write_key"]:
            raise ShellerNotOwner("not_owner", status=403)

        return session
