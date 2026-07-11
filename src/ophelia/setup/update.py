"""Self-update: git pull + reinstall, with optional deferred restart.

Used by ``ophelia update`` and the owner-only Telegram ``/update`` command so
you can ship fixes without sitting at the phone/PC terminal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from ophelia.platform import is_termux

log = structlog.get_logger()

_TMUX_SESSION = "ophelia"


@dataclass
class UpdateResult:
    ok: bool
    repo: Path | None = None
    branch: str | None = None
    before_sha: str | None = None
    after_sha: str | None = None
    changed: bool = False
    steps: list[str] = field(default_factory=list)
    error: str | None = None
    restart_scheduled: bool = False

    def summary(self, *, max_chars: int = 3500) -> str:
        lines: list[str] = []
        if self.ok:
            lines.append("Update OK." if self.changed else "Already up to date.")
        else:
            lines.append(f"Update failed: {self.error or 'unknown error'}")
        if self.repo:
            lines.append(f"Repo: {self.repo}")
        if self.branch:
            lines.append(f"Branch: {self.branch}")
        if self.before_sha or self.after_sha:
            before = (self.before_sha or "?")[:8]
            after = (self.after_sha or "?")[:8]
            lines.append(f"Commit: {before} → {after}")
        if self.steps:
            lines.append("")
            lines.extend(self.steps[-12:])
        if self.restart_scheduled:
            lines.append("")
            lines.append("Restart scheduled — I'll be back in a few seconds.")
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 3] + "..."


def find_repo_root(start: Path | None = None) -> Path | None:
    """Locate the Ophelia git checkout.

    Prefers walking up from the installed package (editable ``pip install -e .``
    layout: ``…/src/ophelia/__init__.py`` → repo root). Falls back to cwd.
    """
    candidates: list[Path] = []
    if start is not None:
        candidates.append(start.resolve())
    try:
        import ophelia

        pkg = Path(ophelia.__file__).resolve().parent
        # src/ophelia -> repo ; or site-packages/ophelia (no .git)
        candidates.append(pkg.parent.parent)  # …/src → repo
        candidates.append(pkg.parent)  # flat layout
    except Exception:
        pass
    candidates.append(Path.cwd().resolve())

    seen: set[Path] = set()
    for base in candidates:
        cur = base
        for _ in range(8):
            if cur in seen:
                break
            seen.add(cur)
            if (cur / ".git").exists() and (
                (cur / "pyproject.toml").is_file() or (cur / "src" / "ophelia").is_dir()
            ):
                return cur
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: float = 300.0,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s: {' '.join(args)}"
    except FileNotFoundError as e:
        return 127, str(e)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    combined = out if not err else (out + "\n" + err if out else err)
    return proc.returncode, combined.strip()


def _git(repo: Path, *args: str, timeout: float = 120.0) -> tuple[int, str]:
    return _run(["git", *args], cwd=repo, timeout=timeout)


def _head_sha(repo: Path) -> str | None:
    rc, out = _git(repo, "rev-parse", "HEAD")
    return out.strip() if rc == 0 and out.strip() else None


def _current_branch(repo: Path) -> str | None:
    rc, out = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return None
    name = out.strip()
    return None if not name or name == "HEAD" else name


def _working_tree_dirty(repo: Path) -> bool:
    rc, out = _git(repo, "status", "--porcelain")
    return rc == 0 and bool(out.strip())


def _pip_install(repo: Path) -> tuple[int, str]:
    pip = shutil.which("pip") or shutil.which("pip3")
    if not pip:
        # Fall back to python -m pip
        return _run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "-q"],
            cwd=repo,
            timeout=600.0,
        )
    return _run([pip, "install", "-e", ".", "-q"], cwd=repo, timeout=600.0)


def schedule_restart(*, delay_sec: float = 3.0) -> bool:
    """Detach a process that restarts Ophelia after ``delay_sec``.

    Termux: stop/start the ``ophelia`` tmux session (no TUI prompts).
    Elsewhere: start a new ``ophelia run --restart`` after this process exits
    (caller should exit soon so the run-lock is released).
    """
    delay = max(1.0, float(delay_sec))
    python = sys.executable
    if is_termux():
        # Run outside the current tmux session so killing it doesn't kill us first.
        prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
        path_export = f'export PATH="{prefix}/bin:$PATH"'
        # Resolve install cwd from repo if possible
        repo = find_repo_root()
        cwd = str(repo) if repo else str(Path.cwd())
        script = (
            f"{path_export}; "
            f"sleep {delay:.1f}; "
            f"tmux kill-session -t {_TMUX_SESSION} 2>/dev/null || true; "
            f"sleep 1; "
            f"termux-wake-lock 2>/dev/null || true; "
            f"tmux new-session -d -s {_TMUX_SESSION} "
            f"'{path_export}; cd {cwd} && ophelia run --restart'"
        )
        try:
            subprocess.Popen(
                ["bash", "-lc", script],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info("update.restart_scheduled", mode="termux-tmux", delay=delay)
            return True
        except Exception as e:
            log.warning("update.restart_schedule_failed", error=str(e))
            return False

    # PC / server: new process waits for our lock to clear, then runs.
    script = f"sleep {delay:.1f}; exec {python} -m ophelia run --restart"
    try:
        subprocess.Popen(
            ["bash", "-lc", script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(find_repo_root() or Path.cwd()),
        )
        log.info("update.restart_scheduled", mode="exec-run", delay=delay)
        return True
    except Exception as e:
        log.warning("update.restart_schedule_failed", error=str(e))
        return False


def run_update(
    *,
    branch: str | None = None,
    restart: bool = False,
    allow_dirty: bool = False,
    remote: str = "origin",
) -> UpdateResult:
    """Pull latest code and reinstall the package.

    ``branch`` — if set, checkout/track that branch before pulling.
    ``restart`` — schedule a deferred process restart after success.
    ``allow_dirty`` — proceed even with local uncommitted changes (uses
    ``git pull --rebase --autostash`` when dirty).
    """
    result = UpdateResult(ok=False)
    repo = find_repo_root()
    if repo is None:
        result.error = (
            "Couldn't find an Ophelia git checkout. "
            "Run from the repo, or reinstall with: pip install -e /path/to/Ophelia"
        )
        return result
    result.repo = repo

    if shutil.which("git") is None:
        result.error = "git is not installed / not on PATH"
        return result

    dirty = _working_tree_dirty(repo)
    if dirty and not allow_dirty:
        result.error = (
            "Working tree has uncommitted changes. "
            "Commit/stash them, or pass --allow-dirty / `/update dirty`."
        )
        result.steps.append("git status: dirty")
        return result

    result.before_sha = _head_sha(repo)
    result.branch = branch or _current_branch(repo)

    # Fetch
    rc, out = _git(repo, "fetch", remote, "--prune", timeout=180.0)
    result.steps.append(f"git fetch {remote}: " + ("ok" if rc == 0 else out[:200]))
    if rc != 0:
        result.error = f"git fetch failed: {out[:500]}"
        return result

    if branch:
        # Checkout requested branch (create tracking branch if needed)
        rc, out = _git(repo, "checkout", branch)
        if rc != 0:
            rc, out = _git(
                repo, "checkout", "-B", branch, f"{remote}/{branch}"
            )
        result.steps.append(
            f"git checkout {branch}: " + ("ok" if rc == 0 else out[:200])
        )
        if rc != 0:
            result.error = f"git checkout {branch} failed: {out[:500]}"
            return result
        result.branch = branch

    pull_args = ["pull", "--ff-only", remote]
    cur = _current_branch(repo)
    if cur:
        pull_args.append(cur)
    if dirty and allow_dirty:
        pull_args = ["pull", "--rebase", "--autostash", remote] + (
            [cur] if cur else []
        )

    rc, out = _git(repo, *pull_args, timeout=180.0)
    snippet = (out or "ok")[:240].replace("\n", " | ")
    result.steps.append(f"git {' '.join(pull_args)}: {snippet}")
    if rc != 0:
        result.error = f"git pull failed: {out[:500]}"
        return result

    result.after_sha = _head_sha(repo)
    result.changed = bool(
        result.before_sha and result.after_sha and result.before_sha != result.after_sha
    ) or ("Already up to date" not in (out or "") and "up to date" not in (out or "").lower())

    # Reinstall so console scripts / package code match the tree
    rc, out = _pip_install(repo)
    result.steps.append(
        "pip install -e .: " + ("ok" if rc == 0 else (out or "failed")[:240])
    )
    if rc != 0:
        result.error = f"pip install failed: {out[:500]}"
        return result

    result.ok = True
    # If SHAs equal and pull said up to date, mark unchanged
    if result.before_sha and result.after_sha and result.before_sha == result.after_sha:
        result.changed = False

    if restart:
        result.restart_scheduled = schedule_restart(delay_sec=3.0)
        if not result.restart_scheduled:
            result.steps.append(
                "Could not schedule restart — run `ophelia restart` (Termux) "
                "or restart the process manually."
            )
    return result


def request_process_exit_soon(delay_sec: float = 2.5) -> None:
    """Exit this process after a short delay so a scheduled restart can take the lock.

    Safe to call from a Telegram handler after the reply is sent. On Termux the
    tmux kill from ``schedule_restart`` usually ends us first; this is a backup
    for PC/server runs.
    """

    def _exit() -> None:
        time.sleep(max(0.5, delay_sec))
        log.info("update.process_exit")
        os._exit(0)

    import threading

    threading.Thread(target=_exit, name="ophelia-update-exit", daemon=True).start()
