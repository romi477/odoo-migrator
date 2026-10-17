"""In-memory Sheller. Same interface as HttpShellerClient; no daemon required."""

import hashlib
import json
import re
import secrets
import uuid

from odoo_migrator.sheller.client import with_js_capability
from odoo_migrator.sheller.errors import (
    ShellerBusy,
    ShellerError,
    ShellerNeedsHuman,
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
# Sheller 1.8.3 takes a bare module as well, and lists what failed.
MODULE_SPEC_RE = re.compile(r"^(\w+)(?:\.(\w+)(?:\.(\w+))?)?$")
MODULE_RUNS_SINCE = (1, 8, 3)
# Sheller 1.8.3 refuses an agent's open with `allow_commit: true`: a grant is
# a person's act. From then until 1.11.0 the runner's target side could not
# open at all. 1.11.0 lets a person switch a container on for every agent's
# commit, and the open is accepted there.
NEEDS_A_HUMAN_SINCE = (1, 8, 3)
AGENT_COMMIT_SINCE = (1, 11, 0)
HELP_SINCE = (1, 11, 0)

# What `GET /api/help` names as replaceable: the MCP tools without `os_`.
HELP_MARKERS = [
    "exec", "source", "journal", "history", "run_test", "test_result", "list_tests",
    "open_session", "close_session", "commit", "rollback", "interrupt", "session",
    "attach_session", "help", "list_sessions", "list_containers",
]

# Topics as the daemon serves them, cut short but not invented: the wording is
# Sheller 1.11.0's, markers and all. One of each kind a client has to tell
# apart — a topic it takes as served, one it owns, one that is about a flow it
# does not have, and one whose text names a tool it has no equivalent of.
HELP_TOPICS = {
    "limits": (
        "## What you cannot do, and must not attempt\n\n"
        "- Read or write ~/.odoo-sheller/ directly. Everything you need is in these tools.\n"
        "- Count on Odoo 13 or 14. They open, with a warning, but are not fully tested:\n"
        "  sessions, exec, commit, rollback and interrupt should work; {{run_test}} does\n"
        "  not on 13 and says so.\n"
    ),
    "orm": (
        "## Writing idiomatic ORM code\n\n"
        "`records.mapped('name')` returns each record's `name` as a plain list.\n"
    ),
    "code": (
        "## Reading the code you are debugging\n\n"
        "Reading source is one tool call, not a shell:\n\n"
        "    {{source}}(path=\"integration_shopify\")\n\n"
        "When a read is not enough, that goes in {{exec}}.\n"
    ),
    "log": (
        "## Seeing what Odoo logged\n\n"
        "Read them with {{journal}}, which has every line in order. A whole module "
        "leaves its log in the journal: {{help}}('tests').\n"
    ),
    "modules": (
        "## Installing and updating a module\n\n"
        "For a change in a `.py`, close this session and open a new one — "
        "{{help}}('sessions').\n"
    ),
    "tests": (
        "## Running a test\n\n"
        "{{run_test}} takes three forms, and the run opens a session of its own: do not "
        "call {{close_session}} on it. {{test_result}} re-reads the last one.\n"
    ),
    "sessions": (
        "## Sessions\n\n{{open_session}} opens one and {{close_session}} closes it.\n"
    ),
    "commit": "## Commit\n\nCommit is a right the human grants: {{commit}}.\n",
    "odoo_17": "## Odoo 17\n\nUse {{source}} before you rely on a name.\n",
}


def _version(text: str) -> tuple[int, ...]:

    return tuple(int(piece) for piece in text.split(".") if piece.isdigit())

class FakeShellerClient:
    def __init__(
        self,
        *,
        version: str = "1.11.0",
        container: bool = False,
        mcp: dict | None = None,
        agent_commit: set[str] | None = None,
    ):
        self._version = version
        self._module_runs = _version(version) >= MODULE_RUNS_SINCE
        self._needs_a_human = _version(version) >= NEEDS_A_HUMAN_SINCE
        self._has_switch = _version(version) >= AGENT_COMMIT_SINCE
        self._container = container
        self._mcp = mcp
        self.help_topics: dict[str, str] = dict(HELP_TOPICS)
        # What a session reports about running a browser suite (1.11.0), and
        # what the module listing says of a module's JS tests, by module.
        self.js_tests: dict = {
            "runner": "hoot", "available": True, "reason": None, "tested": "18-19",
        }
        self.js_listing: dict[str, dict] = {}
        # spec -> the `hoot` block of that run's answer. A skipped one fails
        # the run, as the daemon makes it.
        self.hoot_answers: dict[str, dict] = {}
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
        # Containers a person switched on for agents (*agents may commit*).
        # Both by default, as a migration is set up to run: the source side
        # opens with `allow_commit: false`, and the switch honours that.
        self._agent_commit: dict[str, str] = {
            name: "2026-10-16T00:00:00Z"
            for name in (
                agent_commit
                if agent_commit is not None
                else {entry["name"] for entry in self._containers}
            )
        }
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
        # Specs whose run outlives the call: `run_test` raises ShellerTimeout
        # and the run stays busy in the session until interrupted, as a
        # command in `hangs_on` does.
        self.test_hangs: set[str] = set()
        self.rollbacks: list[str] = []

    def health(self) -> dict:

        return {
            "ok": True,
            "version": self._version,
            "container": self._container,
            "mcp": self._mcp,
        }

    def help(self) -> dict:
        """``GET /api/help``: every topic with its hash, markers unresolved.

        1.10.0 and older have no such route and answer 404. `help_topics` is a
        copy per instance, so a test can add a topic the way Sheller would.
        """
        if _version(self._version) < HELP_SINCE:
            raise ShellerError("Sheller HTTP 404: Not Found", status=404)

        return {
            "version": self._version,
            "instructions": "odoo-sheller runs Python in a live Odoo shell.",
            "topics": {
                name: {"text": text, "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                for name, text in self.help_topics.items()
            },
            "markers": list(HELP_MARKERS),
        }

    def list_containers(self) -> list:

        return [dict(entry) for entry in self._containers]

    def agent_commit(self) -> list:
        """``GET /api/agent-commit``: the switched-on containers, by name."""
        if not self._has_switch:
            raise ShellerError("Not Found", status=404)

        return [
            {"container": name, "since": since}
            for name, since in sorted(self._agent_commit.items())
        ]

    def set_agent_commit(self, container: str, on: bool) -> list[str]:
        """A person flips the switch in Sheller's UI. Not on the client: the
        daemon takes it only with the admin key, which this app never sends.

        As the daemon does, it reaches open sessions at once: on grants every
        agent session on the container that did not opt out, off takes the
        right from every agent session there, whatever gave it.
        """
        if on:
            self._agent_commit.setdefault(container, "2026-10-16T00:00:00Z")
        else:
            self._agent_commit.pop(container, None)
        changed = []
        for session in self._sessions.values():
            if session["container"] != container or session["owner"].get("kind") != "agent":
                continue
            if on and not session["allow_commit"] and not session["opted_out"]:
                session["allow_commit"], session["commit_grant"] = True, "container"
                changed.append(session["id"])
            elif not on and session["allow_commit"]:
                session["allow_commit"], session["commit_grant"] = False, None
                changed.append(session["id"])

        return changed

    def _commit_right(
        self, container: str, owner: dict | None, allow_commit: bool | None
    ) -> tuple[bool, str | None]:
        """What the daemon gives a session at open: the right, and where it came from.

        A local human holds it and needs no grant. An agent gets it from the
        switch alone: `true` where the switch is off is refused, and `false`
        is an opt-out the switch honours.
        """
        if (owner or {}).get("kind", "human") != "agent":
            right = True if allow_commit is None else allow_commit

            return right, None
        if not self._needs_a_human:

            return bool(allow_commit), None
        switched = self._has_switch and container in self._agent_commit
        if allow_commit and not switched:
            raise ShellerNeedsHuman(
                "needs_a_human: open an agent's session with commit already granted — "
                "open it without allow_commit; a human grants the right later, from the "
                "UI — or turns on 'agents may commit' for this container, and its "
                "sessions open with it",
                status=403,
            )
        if allow_commit is False or not switched:

            return False, None

        return True, "container"

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
        right, grant = self._commit_right(container, owner, allow_commit)
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
            "allow_commit": right,
            "commit_grant": grant,
            "opted_out": (owner or {}).get("kind") == "agent" and allow_commit is False,
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
        "commit_grant",
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

        described = {key: value for key, value in session.items() if key in self._DESCRIBED}
        if _version(self._version) >= HELP_SINCE:
            described["js_tests"] = dict(self.js_tests)

        return described

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
        params: str | None = None,
    ) -> dict:
        """Refuse a malformed spec, and answer a run the way the daemon does.

        The spec rule is `module.TestClass` or `module.TestClass.test_method`,
        and from 1.8.3 a bare `module` too — a looser double let
        `module.Class.{'name': ...}` through. The answer is the daemon's: a
        red run is `error: None` with `test.success` false. It used to be
        `{"ok": True}`, and the step that read it counted every failing test
        as a pass. From 1.8.3 every answer carries `failed`, one spec per
        failing test, ready to run again.
        """
        session = self._require(session_id, write_key)
        rule = MODULE_SPEC_RE if self._module_runs else TEST_SPEC_RE
        if not rule.match(test or ""):
            raise ShellerError(
                "invalid_test_spec",
                status=400,
                detail={
                    "error": "invalid_test_spec",
                    "test": test,
                    "recovery": (
                        "use 'module', 'module.TestClass' or 'module.TestClass.test_method'"
                        if self._module_runs
                        else "use 'module.TestClass' or 'module.TestClass.test_method'"
                    ),
                },
            )
        self.tests_run.append(
            {"test": test, "session_id": session_id, "timeout": timeout, "params": params}
        )
        if test in self.test_hangs:
            self._next_command += 1
            session["commands"].append(
                {"kind": "run_test", "id": self._next_command, "test": test, "status": "running"}
            )
            session["hung_polls"] = 0

            raise ShellerTimeout(f"no answer within the timeout for {session_id}")
        # Odoo's runner rolls back whatever the session held before testing.
        discarded = session["pending_commands"] > 0
        session["pending_commands"] = 0
        module, test_class, test_method = rule.match(test).groups()
        outcome = {
            "module": module,
            "test_class": test_class,
            "test_method": test_method,
            "skipped": 0,
            **self._outcome(module, test),
        }
        stderr = list(outcome.pop("stderr", []))
        failed = outcome.pop("failed")
        answer = {
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
        if self._module_runs:
            answer["failed"] = failed
        hoot = self.hoot_answers.get(test)
        if hoot is not None and _version(self._version) >= HELP_SINCE:
            answer["hoot"] = dict(hoot)
            if hoot.get("skipped"):
                # Odoo skips a browser test it cannot run, and a skip counts as
                # a pass; 1.11.0 books it as the failure it is.
                outcome["success"] = False
                answer["error"] = "js_tests_skipped"

        return answer

    def _outcome(self, module: str, spec: str) -> dict:
        """A spec's counts and what in it failed, as the daemon's runner sees
        them: a class runs every method in it and a module every class, the
        counts are theirs, and one red test makes the whole run red.

        `test_outcomes[spec]` is laid over the result, `failed` included —
        how a test makes a fixture fail outside any test method.
        """
        parts = spec.count(".")
        if parts == 0:
            children = [item.get("spec") for item in self._catalogues.get(module, [])]
        elif parts == 1:
            children = []
            for item in self._catalogues.get(module, []):
                if item.get("spec") == spec:
                    children = [method["spec"] for method in item.get("methods") or []]
        else:
            children = []
        merged = {"tests_run": 1, "failures": 0, "errors": 0, "success": True, "stderr": []}
        failed: list[dict] = []
        if children:
            merged["tests_run"] = 0
            for child in children:
                one = self._outcome(module, child)
                merged["tests_run"] += one.get("tests_run", 1)
                merged["failures"] += one.get("failures", 0)
                merged["errors"] += one.get("errors", 0)
                merged["success"] = merged["success"] and one.get("success", True)
                merged["stderr"].extend(one.get("stderr", []))
                failed.extend(one["failed"])
        merged.update(self.test_outcomes.get(spec, {}))
        if "failed" not in self.test_outcomes.get(spec, {}):
            if not merged["success"] and not failed:
                kind = "error" if merged.get("errors") else "failure"
                failed = [{"test": spec, "kind": kind}]
            merged["failed"] = failed

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

        return with_js_capability(
            self.catalogue_route(session["container"], module), self.get_session(session_id)
        )

    def catalogue_route(self, container: str, module: str) -> dict:
        """What `GET /api/containers/{container}/tests` answers: files on disk,
        not a session's. The JS part exists from 1.11.0."""
        answer = {
            "module": module,
            "path": f"/opt/project/{module}",
            "classes": list(self._catalogues.get(module, [])),
            "container": container,
        }
        if _version(self._version) >= HELP_SINCE:
            answer["js"] = dict(
                self.js_listing.get(module) or {"runner": "hoot", "files": [], "run": None}
            )

        return answer

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
