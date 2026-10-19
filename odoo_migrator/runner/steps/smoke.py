"""Use the ported module the way a person would, before the verdict.

Its tests prove what they assert and nothing else, and they are only Python. A
person opening the module does more: a new record's form, a list of existing
ones, an action, a print, and the browser loading what the module put in an
asset bundle. Odoo checks views and references when it installs a module; it
does not compile the bundles, compute the fields, evaluate an action's domain
or render a report. This step does, without a model: one script, the same for
every module, that asks the database what this module registered and tries
each thing. Nothing is written: every check runs under a savepoint, and the
session is rolled back after.

A problem is the module's when it is about something the module declared, or
when the traceback runs through its files: that fails the lap, and the failure
lane gets the list. A bundle broken by another module in a shared database is
not this port's to fix: that is a finding for the review.
"""

from __future__ import annotations

import ast
import json
import time

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult, check_stop
from odoo_migrator.runner.sessions import ensure_usable
from odoo_migrator.runner.tools import _elapsed, _record
from odoo_migrator.sheller.errors import ShellerError, ShellerTimeout

# Compiling a large bundle takes seconds; a module's own computes on twenty
# records should take less. The daemon's ceiling is 300s.
SMOKE_TIMEOUT = 330.0
# What the failure lane reads of each problem: enough to place it.
_SHOWN = 20

# Doubled braces: this is a `str.format` template, filled with the module's name.
# Raw: the script's own `'\n'` must reach the daemon as written.
_SCRIPT = r"""import fnmatch as _fnmatch
import json as _json
import traceback as _traceback

from odoo.exceptions import UserError as _UserError
from odoo.tools.safe_eval import safe_eval as _safe_eval

_name = {name!r}
_found = []
_seen = {{'models': 0, 'fields': 0, 'new': 0, 'read': 0, 'actions': 0, 'reports': 0, 'bundles': 0}}


def _own(text):
    return ('/%s/' % _name) in text


def _check(where, call, own=None):
    # A UserError is the module saying no, the way it says it to a person:
    # a wizard opened without its record, a state that refuses. Not a break.
    try:
        with env.cr.savepoint():
            call()
    except _UserError:
        pass
    except Exception as exc:
        trace = _traceback.format_exc()
        _found.append({{
            'where': where,
            'error': '%s: %s' % (type(exc).__name__, str(exc).strip().splitlines()[0][:300] if str(exc).strip() else ''),
            'own': _own(trace) if own is None else own,
            'trace': '\n'.join(trace.strip().splitlines()[-12:]),
        }})


_rows = env['ir.model.data'].sudo().search([('module', '=', _name)])
_ids = {{}}
for _row in _rows:
    _ids.setdefault(_row.model, []).append(_row.res_id)

# The fields this module declares, by model: for a model it extends, its own
# fields; for one it defines, all of them.
_fields = {{}}
for _field in env['ir.model.fields'].sudo().browse(_ids.get('ir.model.fields', [])).exists():
    _fields.setdefault(_field.model, []).append(_field.name)

for _model in sorted(_fields):
    if _model not in env or env[_model]._abstract:
        continue
    _Model = env[_model]
    _names = [n for n in _fields[_model] if n in _Model._fields]
    _seen['models'] += 1
    _seen['fields'] += len(_names)

    # "New": the defaults a form opens with, and every field of the module's
    # computed on that new record, as the form shows them.
    def _new(_Model=_Model, _names=_names):
        _record = _Model.new(_Model.default_get(list(_Model._fields)))
        for _n in _names:
            _record[_n]
    _check('%s: a new record (defaults and computed fields)' % _model, _new, own=True)
    _seen['new'] += 1

    # "Open the list": the module's fields read on the latest records.
    def _read(_Model=_Model, _names=_names):
        _records = _Model.search([], limit=20, order='id desc')
        _records.read(_names)
        _records.mapped('display_name')
    _check('%s: reading its fields on existing records' % _model, _read, own=True)
    _seen['read'] += 1

# Window actions: the domain the client sends, evaluated and searched. A
# field a domain names is not checked when the module installs.
_evaluation = {{'uid': env.uid, 'user': env.user, 'context': dict(env.context),
               'active_id': False, 'active_ids': [], 'allowed_company_ids': env.companies.ids,
               'current_company_id': env.company.id}}
for _action in env['ir.actions.act_window'].sudo().browse(_ids.get('ir.actions.act_window', [])).exists():
    _seen['actions'] += 1
    if _action.res_model not in env:
        _found.append({{'where': 'action %s' % _action.name, 'error': 'its model %s does not exist' % _action.res_model, 'own': True, 'trace': ''}})
        continue
    try:
        _domain = _safe_eval(_action.domain or '[]', dict(_evaluation))
    except Exception:
        continue  # it names something only the client has
    _check('action %s: its domain %s' % (_action.name, _action.domain),
           lambda _action=_action, _domain=_domain: env[_action.res_model].search_count(_domain), own=True)

# Reports: rendered as HTML on one record, the way a print starts.
_Report = env['ir.actions.report']
for _report in _Report.sudo().browse(_ids.get('ir.actions.report', [])).exists():
    if _report.report_type not in ('qweb-html', 'qweb-pdf') or _report.model not in env:
        continue
    _record_ids = env[_report.model].search([], limit=1, order='id desc').ids
    if not _record_ids:
        continue
    _seen['reports'] += 1
    _check('report %s' % _report.report_name,
           lambda _report=_report, _record_ids=_record_ids: _Report._render_qweb_html(_report.report_name, _record_ids),
           own=True)

# Assets: what the module adds to a bundle is not compiled when it installs.
# Compiled here past the cache, which hands back the last good attachment.
try:
    from odoo.modules.module import get_manifest as _get_manifest
    _assets = (_get_manifest(_name) or {{}}).get('assets') or {{}}
except Exception:
    _assets = {{}}
for _bundle_name, _entries in sorted(_assets.items()):
    _seen['bundles'] += 1
    try:
        try:
            _bundle = env['ir.qweb']._get_asset_bundle(_bundle_name, css=True, js=True)
        except TypeError:
            # 16.0 takes the files, which 17.0 looks up itself.
            _files, _ = env['ir.qweb']._get_asset_content(_bundle_name)
            _bundle = env['ir.qweb']._get_asset_bundle(_bundle_name, _files)
    except Exception as exc:
        _found.append({{'where': 'bundle %s' % _bundle_name, 'error': '%s: %s' % (type(exc).__name__, exc), 'own': _own(str(exc)), 'trace': ''}})
        continue
    _urls = [f.get('url', '') for f in getattr(_bundle, 'files', [])]
    for _entry in _entries:
        _path = _entry if isinstance(_entry, str) else (_entry[-1] if _entry and isinstance(_entry[-1], str) else '')
        if _path.startswith(_name + '/') and not any(_fnmatch.fnmatch(u.lstrip('/'), _path) for u in _urls):
            _found.append({{'where': 'bundle %s' % _bundle_name, 'error': 'the manifest lists %s and no file matches it' % _path, 'own': True, 'trace': ''}})
    try:
        _bundle.preprocess_css()
    except Exception as exc:
        _bundle.css_errors.append('%s: %s' % (type(exc).__name__, exc))
    for _error in getattr(_bundle, 'css_errors', []) or []:
        _found.append({{'where': 'bundle %s: styles' % _bundle_name, 'error': str(_error).strip()[:600], 'own': _own(str(_error)), 'trace': ''}})
    try:
        _blocks = _bundle.xml() if hasattr(_bundle, 'xml') else []
    except Exception as exc:
        _found.append({{'where': 'bundle %s: templates' % _bundle_name, 'error': '%s: %s' % (type(exc).__name__, exc), 'own': _own(str(exc)), 'trace': ''}})
        _blocks = []
    _names_defined = set()
    _extends = []
    for _block in _blocks:
        if _block.get('type') == 'templates':
            for _element, _url, _parent in _block['templates']:
                _names_defined.add(_element.get('t-name'))
                if _parent:
                    _extends.append((_parent, _url))
        else:
            for _parent, _elements in _block.get('extensions', {{}}).items():
                _extends.extend((_parent, _url) for _element, _url in _elements)
    for _parent, _url in _extends:
        if _parent not in _names_defined and _own(_url):
            _found.append({{'where': 'bundle %s: templates' % _bundle_name, 'error': '%s extends %s, which this bundle does not define' % (_url, _parent), 'own': True, 'trace': ''}})

_json.dumps({{'checked': _seen, 'problems': _found}})
"""


def run(step: Step, ctx: StepContext) -> StepResult:
    check_stop(ctx, "before smoke")
    name = ctx.module.technical_name
    session_id = ctx.sessions.get("target", "")
    write_key = ctx.write_keys.get("target", "")
    if not session_id:

        return StepResult(ok=False, output="no target session")
    started = time.perf_counter()
    try:
        executed = ctx.sheller.exec(
            session_id, _SCRIPT.format(name=name), write_key=write_key, timeout=SMOKE_TIMEOUT
        )
    except ShellerTimeout:
        _record(ctx, "smoke", "target", session_id, "", _elapsed(started), False, f"{name} · ran past {SMOKE_TIMEOUT:.0f}s")
        freed = _free(ctx)

        return _unrun(ctx, f"the checks ran past {SMOKE_TIMEOUT:.0f}s{freed}")
    except ShellerError as exc:
        _record(ctx, "smoke", "target", session_id, "", _elapsed(started), False, f"{name} · {exc}")

        return _unrun(ctx, str(exc))
    finally:
        _roll_back(ctx)
    command_id = str(executed.get("id") or "")
    report = _report(executed)
    if report is None:
        error = executed.get("error")
        said = f"{error.get('type')}: {error.get('message')}" if isinstance(error, dict) else str(error or "no answer")
        _record(ctx, "smoke", "target", session_id, command_id, _elapsed(started), False, f"{name} · {said}")

        return _unrun(ctx, said)
    problems = report.get("problems") or []
    own = [problem for problem in problems if problem.get("own")]
    for problem in problems:
        if problem.get("own"):
            continue
        finding = {"text": f"{problem.get('where')}: {problem.get('error')}", "source": "smoke: another module"}
        if finding not in ctx.findings:
            ctx.findings.append(finding)
    checked = _checked(report.get("checked") or {})
    said = f"{checked}; {len(own)} problem(s)" if own else f"{checked}; nothing broke"
    _record(ctx, "smoke", "target", session_id, command_id, _elapsed(started), not own, f"{name} · {said}")
    if not own:

        return StepResult(ok=True, output=f"{name}: {said}")
    lines = [f"Using {name} the way a person would broke {len(own)} time(s) ({checked}):"]
    for problem in own[:_SHOWN]:
        lines.append(f"- {problem.get('where')}: {problem.get('error')}")
        trace = (problem.get("trace") or "").strip()
        if trace:
            lines.append("  " + trace.replace("\n", "\n  "))
    if len(own) > _SHOWN:
        lines.append(f"(and {len(own) - _SHOWN} more)")

    return StepResult(ok=False, output="\n".join(lines))


def _report(executed: dict) -> dict | None:
    """The script's last line is a JSON string; the daemon hands back its repr."""
    if executed.get("error"):

        return None
    try:
        report = json.loads(ast.literal_eval(executed.get("result") or ""))
    except (ValueError, SyntaxError, TypeError):

        return None

    return report if isinstance(report, dict) else None


def _checked(checked: dict) -> str:
    """`2 models, 13 fields, 1 bundle`: what was tried, so "nothing broke"
    over nothing at all reads as what it is."""
    parts = []
    for key, one, many in (
        ("models", "model", "models"),
        ("fields", "field", "fields"),
        ("actions", "action", "actions"),
        ("reports", "report", "reports"),
        ("bundles", "bundle", "bundles"),
    ):
        count = int(checked.get(key) or 0)
        if count:
            parts.append(f"{count} {one if count == 1 else many}")

    return ", ".join(parts) or "nothing of its own to try"


def _unrun(ctx: StepContext, reason: str) -> StepResult:
    """The checks did not run to the end. That says nothing about the module,
    and the lap is not failed on it: the review is told instead."""
    finding = {"text": f"The smoke checks did not run: {reason}", "source": "smoke"}
    if finding not in ctx.findings:
        ctx.findings.append(finding)

    return StepResult(ok=True, output=f"not run: {reason}")


def _roll_back(ctx: StepContext) -> None:
    try:
        ctx.sheller.rollback(ctx.sessions.get("target", ""), write_key=ctx.write_keys.get("target", ""))
    except ShellerError:
        pass


def _free(ctx: StepContext) -> str:
    try:
        done = ensure_usable(ctx, "target")
    except ShellerError as exc:

        return f"; the session could not be freed: {exc}"

    return f"; {done}" if done else ""
