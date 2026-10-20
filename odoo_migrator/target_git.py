"""Keep the target under git, and commit each module the agent ports.

Git is the review and the undo of a port written in place, and the hand-edit
guard reads it. A target that was no repository had neither: a re-run wrote
over a person's edits without a word. So a target outside git is put under
it on the first start, with a baseline of the plan's modules as they were,
and every module that passes is committed on its own — one module, one
commit, in its own directory and nothing beside it.

Commits are the app's, not the person's: a fixed author, no signature asked
for, no hooks run. A hook that formats files would rewrite the port the
tests just passed, and a signing prompt would hang a background thread.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from odoo_migrator.db import database
from odoo_migrator.models import Migration, Module
from odoo_migrator.module_paths import MANIFEST, module_root

AUTHOR = ("odoo-migrator", "odoo-migrator@localhost")
BASELINE_MESSAGE = "odoo-migrator: baseline before porting"
# What Odoo leaves beside a module it imported. Not anyone's work.
IGNORED = ("__pycache__/", "*.py[co]")
_EXCLUDED = tuple(f":(exclude,glob)**/{pattern}**" if pattern.endswith("/") else
                  f":(exclude,glob)**/{pattern}" for pattern in IGNORED)
# Two modules of one batch can finish together, and git takes one index lock
# per repository. Commits wait their turn here rather than fail on it.
_LOCK = threading.Lock()


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:

    return subprocess.run(
        [
            "git",
            "-c", f"user.name={AUTHOR[0]}",
            "-c", f"user.email={AUTHOR[1]}",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=check,
    )


def _toplevel(directory: Path) -> Path | None:
    try:
        answer = _git(directory, "rev-parse", "--show-toplevel", check=False)
    except (OSError, subprocess.TimeoutExpired):

        return None
    if answer.returncode != 0:

        return None

    return Path(answer.stdout.strip())


def ensure_repository(migration_id: str) -> str | None:
    """Put the target project directory under git if nothing holds it yet.

    The baseline holds the plan's module directories only. The rest of the
    directory — a database volume, a filestore, files this user cannot read
    — is not the project's source, and stays out. A module that is its own
    repository stays out too: it already has its history. A repository this
    app made gets the `.gitignore` lines it is missing (`catch_up`). Returns
    what was done, for the run log, or None when there was nothing to do.
    """
    with database.connection_context():
        migration = Migration.get_or_none(Migration.id == migration_id)
        if migration is None:

            return None
        modules = list(
            Module.select().where(
                (Module.migration == migration_id) & (Module.struck_out == False)
            )
        )
        directories = [module_root(migration, module, "target") for module in modules]
        raw = (migration.new_project_path or "").strip()
    if not raw:

        return None
    root = Path(raw).expanduser()
    if not root.is_dir():

        return None
    if _toplevel(root) is not None:

        return catch_up(root, directories, modules=False) or None
    inside, failure = init_with_baseline(root, directories, BASELINE_MESSAGE)
    if failure:

        return failure

    return (
        f"{root} had no repository: git init, and a baseline commit "
        f"of {len(inside)} module director{'y' if len(inside) == 1 else 'ies'} "
        "as they were. Each module that passes is committed on its own."
    )


def init_with_baseline(root: Path, directories, message: str) -> tuple[list[str], str]:
    """`git init` in `root`, ignore what Odoo leaves behind, and commit the
    module directories as they are. Returns those directories (relative to
    `root`) and, when git did not finish, what went wrong.

    A module that is its own repository stays out: it has its history. The
    rest of the project directory stays out too; it is not source, and the
    `.gitignore` says so (`environment`), so it does not sit in `git status`
    as work nobody committed.
    """
    inside = sorted(
        directory.relative_to(root).as_posix()
        for directory in directories
        if directory is not None
        and directory.is_dir()
        and directory.is_relative_to(root)
        and not (directory / ".git").exists()
    )
    with _LOCK:
        try:
            _git(root, "init", "-q")
            ignore = root / ".gitignore"
            if not ignore.exists():
                lines = [*IGNORED, *environment(root, directories)]
                ignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
            _git(root, "add", "--", ".gitignore")
            if inside:
                _git(root, "add", "-A", "--", *inside, *_EXCLUDED)
            _git(root, "commit", "-q", "--no-verify", "--allow-empty", "-m", message)
        except (OSError, subprocess.SubprocessError) as exc:

            return inside, f"git init in {root} did not finish: {_why(exc)}"

    return inside, ""



def _holds_modules(directory: Path) -> bool:
    """A module, or a directory with one up to two levels down."""
    for pattern in (MANIFEST, f"*/{MANIFEST}", f"*/*/{MANIFEST}"):
        if next(directory.glob(pattern), None) is not None:

            return True

    return False


def environment(root: Path, directories) -> list[str]:
    """What the project directory holds beside its modules, as anchored
    `.gitignore` lines: a compose file, an `odoo.conf`, a filestore mounted
    with Odoo's sessions in it.

    The baseline leaves all of it out, and it then showed in `git status` for
    good; one `git add -A` from the top would have put the sessions in
    history. Only the directories on the way down to a plan module are looked
    at. An entry there stays visible when it leads to a plan module or holds
    a module of its own: a module outside the plan is still code.
    """
    planned = {directory for directory in directories if directory is not None}
    keep: set[Path] = set()
    for directory in planned:
        if directory.is_relative_to(root):
            keep.add(directory)
            keep.update(parent for parent in directory.parents if parent.is_relative_to(root))
    found = []
    for level in sorted(keep - planned):
        try:
            entries = sorted(level.iterdir()) if level.is_dir() else []
        except OSError:
            entries = []
        for entry in entries:
            if entry.name in (".git", ".gitignore") or entry in keep:
                continue
            if entry.is_dir() and _holds_modules(entry):
                continue
            path = entry.relative_to(root).as_posix()
            found.append(f"/{path}/" if entry.is_dir() else f"/{path}")

    return found


def made_here(root: Path) -> bool:
    """The repository at `root` is one this app made: its first commit is the
    app's baseline. Only there does the app track more than it was asked to."""
    top = _toplevel(root)
    if top is None or top.resolve() != root.resolve():

        return False
    answer = _git(root, "log", "--max-parents=0", "--format=%ae%x1f%s", "HEAD", check=False)
    first = answer.stdout.strip().splitlines()

    return (
        answer.returncode == 0
        and len(first) == 1
        and first[0].startswith(f"{AUTHOR[1]}\x1fodoo-migrator: baseline")
    )


def untracked_sources(
    root: Path, directories, keep_out=()
) -> tuple[list[str], list[str]]:
    """The plan modules with files git has never seen, and those files, from
    `root`. `keep_out` holds paths (relative to `root`) that are not the
    person's code: on the source side, tests the agent wrote that are not
    proved green yet. Files a person changed are not touched; only new ones
    are listed.
    """
    excluded = [f":(exclude){path}" for path in keep_out]
    names, files = [], []
    for directory in directories:
        if (
            directory is None
            or not directory.is_dir()
            or not directory.is_relative_to(root)
            or (directory / ".git").exists()
        ):
            continue
        relative = directory.relative_to(root).as_posix()
        answer = _git(
            root, "ls-files", "--others", "--exclude-standard", "-z", "--",
            relative, *excluded, *_EXCLUDED,
            check=False,
        )
        new = [path for path in answer.stdout.split("\0") if path]
        if new:
            names.append(directory.name)
            files.extend(new)

    return names, files


def catch_up(root: Path, directories, *, modules: bool, keep_out=()) -> str:
    """In a repository this app made, add what its baseline left untracked.

    The baseline is taken once, of the module directories found at that
    moment. One that was not found then stayed untracked for good, and the
    source branch got tests with no code beside them. So each start adds the
    plan modules' new files (`modules`, the source side: the target's are a
    port, committed when it passes) and the `.gitignore` lines `environment`
    finds missing, in one commit. `keep_out` goes to `untracked_sources`.
    Returns that, for the run log; "" when there was nothing to add.
    """
    if not made_here(root):

        return ""
    with _LOCK:
        try:
            ignore = root / ".gitignore"
            text = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
            present = {line.strip() for line in text.splitlines()}
            missing = [
                line for line in [*IGNORED, *environment(root, directories)]
                if line not in present
            ]
            paths = []
            if missing:
                if text and not text.endswith("\n"):
                    text += "\n"
                ignore.write_text(text + "\n".join(missing) + "\n", encoding="utf-8")
                paths.append(".gitignore")
            names, files = (
                untracked_sources(root, directories, keep_out) if modules else ([], [])
            )
            paths.extend(files)
            if not paths:

                return ""
            _git(root, "add", "--", *paths)
            _git(
                root, "commit", "-q", "--no-verify", "-m", _catch_up_message(names, missing),
                "--", *paths,
            )
            head = _git(root, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:

            return f"{root}: what the baseline missed could not be committed: {_why(exc)}"
    parts = []
    if names:
        parts.append(f"{len(names)} module director{'y' if len(names) == 1 else 'ies'} "
                     f"the baseline missed ({', '.join(sorted(names))})")
    if missing:
        parts.append(f"{len(missing)} .gitignore line{'' if len(missing) == 1 else 's'} "
                     "for what sits beside the modules")

    return f"{root}: committed {' and '.join(parts)} as {head}"


def _catch_up_message(names: list[str], ignored: list[str]) -> str:
    if names:
        title = (
            f"odoo-migrator: baseline of {len(names)} more module "
            f"director{'y' if len(names) == 1 else 'ies'} as they were"
        )
    else:
        title = "odoo-migrator: ignore what the project holds beside its modules"
    lines = [title, ""]
    lines.extend(f"Module: {name}" for name in sorted(names))
    lines.extend(f"Ignored: {line}" for line in ignored)

    return "\n".join(lines)


def commit_module(module: Module, *, job_id: str) -> str | None:
    """Commit what the port left in this module's directory, and only there.

    None when there is nothing to commit, or no repository this app should
    write history into: one at the home directory or above is somebody's
    dotfiles, not this project.
    """
    with database.connection_context():
        migration = Migration.get_by_id(module.migration_id)
        directory = module_root(migration, module, "target")
        target = migration.target_version
    if directory is None or not Path(directory).is_dir():

        return None
    directory = Path(directory)
    top = _toplevel(directory)
    if top is None or _too_broad(top):

        return None
    with _LOCK:
        try:
            _git(directory, "add", "-A", "--", ".", *_EXCLUDED)
            staged = _git(directory, "diff", "--cached", "--quiet", "--", ".", check=False)
            if staged.returncode == 0:

                return None
            message = (
                f"odoo-migrator: port {module.technical_name} to {target}\n\n"
                f"Job {job_id}."
            )
            # `-- .`: the module's paths only. Whatever else a person staged
            # stays staged, and out of this commit.
            _git(directory, "commit", "-q", "--no-verify", "-m", message, "--", ".")
        except (OSError, subprocess.SubprocessError) as exc:

            return f"the port of {module.technical_name} could not be committed: {_why(exc)}"
        head = _git(directory, "rev-parse", "--short", "HEAD", check=False).stdout.strip()

    return f"committed the port of {module.technical_name} as {head}"


def _too_broad(top: Path) -> bool:
    home = Path.home().resolve()
    resolved = top.resolve()

    return resolved == home or resolved in home.parents


def _why(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", "") or ""

    return stderr.strip() or str(exc)


def _last_port(directory: Path) -> str:
    """The newest commit of the app's in this directory: a port, or the
    baseline. "" when there is none."""
    answer = _git(
        directory, "log", "-1", "--format=%H", f"--author={AUTHOR[1]}", "--", ".", check=False
    )

    return answer.stdout.strip() if answer.returncode == 0 else ""


def person_commits(directory: Path) -> list[dict]:
    """Commits by a person in this directory since the app's last one,
    newest first: sha, short, author, subject.

    Empty when the app has no commit here at all: a repository someone
    brought has people's commits all the way down, and none of them came
    after a port.
    """
    directory = Path(directory)
    if not directory.is_dir():

        return []
    try:
        last = _last_port(directory)
        if not last:

            return []
        answer = _git(
            directory, "log", "--format=%H%x00%h%x00%an%x00%s", f"{last}..HEAD", "--", ".",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):

        return []
    commits = []
    for line in answer.stdout.splitlines():
        sha, short, author, subject = (line.split("\0", 3) + ["", "", ""])[:4]
        if sha and author != AUTHOR[0]:
            commits.append({"sha": sha, "short": short, "author": author, "subject": subject})

    return commits


def person_changes(directory: Path) -> str:
    """What people committed in this directory since the app's last commit:
    the commits, then their diff. "" when there is none."""
    commits = person_commits(directory)
    if not commits:

        return ""
    directory = Path(directory)
    try:
        diff = _git(directory, "diff", f"{_last_port(directory)}..HEAD", "--", ".").stdout
    except (OSError, subprocess.SubprocessError) as exc:
        diff = f"(the diff could not be read: {_why(exc)})"
    listed = "\n".join(
        f"{row['short']} {row['author']}: {row['subject']}" for row in commits
    )

    return f"{listed}\n\n{diff.strip()}"

