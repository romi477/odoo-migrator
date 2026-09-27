"""Read Odoo's own source through a session, the way Sheller's `os_source` does.

The daemon has no source route. Its MCP tool builds a snippet, runs it through
`exec`, and parses the JSON string the snippet returns. The migrator is a
client of the HTTP API, not of the MCP server, so it has to do the same. It
used to send the dotted name itself as the code: `sale.order.action_confirm`
is a NameError in any Odoo shell, and the one tool the system prompt calls
"the definitive answer" answered nothing on every live container.

The snippet is odoo-sheller's `_source_snippet` (odoo_sheller/mcp.py, at
69cdcf7), kept to the letter where it touches the container: its comments
record what broke there.
"""

from __future__ import annotations

import ast
import json

from odoo_migrator.sheller.errors import ShellerError

# Agent context is the scarce resource; `first` and `last` page through the rest.
MAX_SOURCE = 8000
MAX_LISTING = 400


def read_source(
    client,
    session_id: str,
    *,
    write_key: str,
    path: str | None = None,
    model: str | None = None,
    method: str | None = None,
    module: str | None = None,
    first: int | None = None,
    last: int | None = None,
) -> dict:
    """A file, a directory listing, or the code a model's method runs."""
    refused = _refusal(path, model, method, module)
    if refused is not None:

        return refused
    code = source_snippet(path, model, method, first, last, module)
    executed = client.exec(session_id, code, write_key=write_key)
    failure = executed.get("error")
    if failure:
        if not isinstance(failure, dict):
            failure = {"type": "Error", "message": str(failure)}

        return {
            "error": "unreadable",
            "reason": f"{failure.get('type')}: {failure.get('message')}",
            "recovery": (
                "a path must be inside an addons directory and a model must "
                "be in the registry"
            ),
        }
    try:
        # The session hands back the repr of the JSON string the snippet
        # returned, which is exactly one literal.
        payload = json.loads(ast.literal_eval(executed.get("result") or "'{}'"))
    except (ValueError, SyntaxError):

        return {"error": "unreadable", "reason": "the read produced no payload"}
    if not isinstance(payload, dict):
        raise ShellerError(f"source read returned {type(payload).__name__}")
    if payload.get("error"):

        return payload
    if payload.get("directory"):

        return _clip_listing(payload)
    text = payload.get("text") or ""
    payload["truncated"] = len(text) > MAX_SOURCE
    if payload["truncated"]:
        payload["text"] = text[:MAX_SOURCE]
        payload["recovery"] = "narrow it with first= and last=, or read one method by model="

    return payload


def _refusal(path, model, method, module) -> dict | None:
    if path and model:

        return {
            "error": "ambiguous_request",
            "recovery": "pass path (a file) or model (the code that runs), not both",
        }
    if not path and not model:

        return {
            "error": "nothing_to_read",
            "recovery": (
                "pass path='module/dir/file.py' with an optional first/last line "
                "range, or model='res.partner' with an optional method"
            ),
        }
    if method and not model:

        return {"error": "method_without_model", "recovery": "pass model= as well"}
    if module and not method:

        return {
            "error": "module_without_method",
            "recovery": (
                "module= picks one link out of a method's override chain, so "
                "name the method too"
            ),
        }

    return None


def _clip_listing(payload: dict) -> dict:
    """A module can hold hundreds of files; a listing is still an answer."""
    entries = payload.get("entries") or []
    payload["truncated"] = len(entries) > MAX_LISTING
    if payload["truncated"]:
        payload["entries"] = entries[:MAX_LISTING]
        payload["recovery"] = (
            f"{len(entries)} files: ask for a subdirectory, e.g. "
            f"path='{payload.get('path')}/models'"
        )

    return payload


def source_snippet(path, model, method, first, last, module=None) -> str:
    """Python for the session to run, returning the payload as a JSON string.

    Everything happens inside one function so its imports and locals never
    reach the session, and the function pops its own name on the way out.
    """
    if path:
        # Python literals, not JSON ones: `json.dumps(None)` is `null`, which
        # is a NameError in the container and nowhere else.
        opening = repr(int(first)) if first else "1"
        closing = repr(int(last)) if last else "len(lines)"
        body = f"""\
    import os
    from odoo.tools import file_open, file_path
    target = file_path({path!r})
    if os.path.isdir(target):
        # Entries are rooted at the module, not at the target's parent, so
        # every path that comes back can be handed straight back in.
        root = target
        while root != os.path.dirname(root):
            if os.path.exists(os.path.join(root, "__manifest__.py")):
                break
            root = os.path.dirname(root)
        base = os.path.dirname(root)
        entries = []
        for root, dirs, names in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(names):
                if name.endswith((".pyc", ".pyo")):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, base)
                try:
                    with open(full, "rb") as handle:
                        count = sum(1 for _ in handle)
                except OSError:
                    count = None
                entries.append({{"path": rel, "lines": count}})
        payload = {{"path": {path!r}, "directory": True,
                   "entries": entries, "files": len(entries)}}
    else:
        with file_open({path!r}) as handle:
            lines = handle.read().splitlines()
        first = {opening}
        last = min({closing}, len(lines))
        payload = {{"path": {path!r}, "total_lines": len(lines),
                   "first": first, "last": last,
                   "text": chr(10).join(lines[first - 1:last])}}
"""
    else:
        # Every class in the MRO extends the model; almost none of them define
        # the method. On account.move.action_post that is 3 classes out of 52.
        body = f"""\
    import inspect
    cls = type(env[{model!r}])
    method = {method!r}
    wanted = {module!r}

    def addon(klass):
        parts = klass.__module__.split(".")
        return parts[2] if klass.__module__.startswith("odoo.addons.") else parts[0]

    def link(klass):
        fn = klass.__dict__[method]
        try:
            line = inspect.getsourcelines(fn)[1]
        except (OSError, TypeError):
            line = None
        return {{"module": addon(klass), "where": klass.__module__,
                "file": inspect.getsourcefile(klass), "line": line}}

    if method:
        definers = [klass for klass in cls.__mro__ if method in klass.__dict__]
        chain = [link(klass) for klass in definers]
        if wanted:
            picked = [klass for klass in definers if addon(klass) == wanted]
            if not picked:
                payload = {{"error": "not_in_that_module", "model": {model!r},
                           "method": method, "module": wanted, "overrides": chain,
                           "recovery": "pick one of the modules in overrides, "
                                       "or drop module= for the one that runs"}}
                return json.dumps(payload)
            obj = picked[0].__dict__[method]
            owner = picked[0]
        else:
            obj = getattr(cls, method)
            owner = definers[0] if definers else cls
        payload = {{"model": {model!r}, "method": method, "module": wanted,
                   "file": inspect.getsourcefile(obj),
                   "line": inspect.getsourcelines(obj)[1],
                   "defined_in": getattr(owner, "__module__", None),
                   "overrides": chain,
                   "text": inspect.getsource(obj)}}
    else:
        payload = {{"model": {model!r}, "method": None,
                   "file": inspect.getsourcefile(cls),
                   "line": inspect.getsourcelines(cls)[1],
                   "defined_in": cls.__module__,
                   "overrides": [addon(klass) for klass in cls.__mro__
                                 if klass.__module__.startswith("odoo.addons.")],
                   "text": inspect.getsource(cls)}}
"""

    return (
        "def _os_read(_os_ns=globals()):\n"
        # First, not last: a read that raises must not leave the name behind.
        '    _os_ns.pop("_os_read", None)\n'
        "    import json\n"
        + body
        + "    return json.dumps(payload)\n"
        "\n"
        "_os_read()"
    )
