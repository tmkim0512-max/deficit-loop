"""Thin git wrapper + ownership rules.

Ownership rule: the kernel only ever deletes branches and worktrees that it
created *and* recorded in its own state. Anything else in the repository is
someone else's and is never touched. If git cannot answer an ownership
question (shallow clone, bare repo, unexpected worktree under our directory)
the kernel stops instead of guessing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT = 120.0


class GitError(RuntimeError):
    pass


class UnsafeRepoError(GitError):
    """Repository state makes an ownership decision impossible -> halt."""


def git(repo: Path, *args: str, check: bool = True, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": _path(), "HOME": _home()},
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from e
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()}")
    return p


def _path() -> str:
    import os
    return os.environ.get("PATH", "/usr/bin:/bin")


def _home() -> str:
    import os
    return os.environ.get("HOME", "/")


def repo_root(path: Path) -> Path:
    p = git(path, "rev-parse", "--show-toplevel", check=False)
    if p.returncode != 0:
        raise GitError(f"{path} is not inside a git worktree: {p.stderr.strip()}")
    return Path(p.stdout.strip()).resolve()


def preflight(repo: Path) -> None:
    """Refuse to run when ownership cannot be judged safely."""
    if git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        raise UnsafeRepoError("shallow clone: history is incomplete, merges and ownership cannot be judged. Run `git fetch --unshallow` and retry")
    if git(repo, "rev-parse", "--is-bare-repository").stdout.strip() == "true":
        raise UnsafeRepoError("bare repository: nothing to measure")
    if git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
        raise UnsafeRepoError("repository has no commits yet")


def head_sha(repo: Path, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", "--verify", ref).stdout.strip()


def branch_exists(repo: Path, name: str) -> bool:
    return git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}", check=False).returncode == 0


def create_branch(repo: Path, name: str, start: str) -> None:
    git(repo, "branch", name, start)


def delete_branch(repo: Path, name: str) -> None:
    git(repo, "branch", "-D", name)


def list_worktrees(repo: Path) -> list[Path]:
    out = git(repo, "worktree", "list", "--porcelain").stdout
    return [Path(line[len("worktree "):]).resolve() for line in out.splitlines() if line.startswith("worktree ")]


def add_worktree(repo: Path, path: Path, branch: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "--quiet", str(path), branch)


def remove_worktree(repo: Path, path: Path) -> None:
    """Unregister and delete exactly this worktree (works even if its directory is already gone).

    A path that git never registered (crash before `worktree add`) is nothing to remove;
    if such a directory nevertheless exists we do not know who made it and refuse.
    """
    if path.resolve() not in list_worktrees(repo):
        if path.exists():
            raise UnsafeRepoError(f"{path} exists but is not a registered worktree; refusing to delete it")
        return
    git(repo, "worktree", "remove", "--force", str(path))


def current_branch(wt: Path) -> str | None:
    p = git(wt, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    return p.stdout.strip() or None


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0


def commit_all(wt: Path, message: str) -> bool:
    """Stage everything in the worktree and commit. Returns False when nothing changed."""
    git(wt, "add", "-A")
    if git(wt, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git(wt, "-c", "user.name=deficit-loop", "-c", "user.email=deficit-loop@localhost",
        "commit", "--quiet", "--no-verify", "-m", message)
    return True


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    out = git(repo, "diff", "--name-only", f"{base}..{head}").stdout
    return [line for line in out.splitlines() if line]


def merge_no_ff(wt: Path, branch: str, message: str) -> bool:
    """Merge *branch* into the worktree's current branch. Returns False (and aborts) on conflict."""
    p = git(wt, "-c", "user.name=deficit-loop", "-c", "user.email=deficit-loop@localhost",
            "merge", "--no-ff", "--no-edit", "-m", message, branch, check=False)
    if p.returncode != 0:
        git(wt, "merge", "--abort", check=False)
        return False
    return True


def reset_hard(wt: Path, sha: str) -> None:
    """Make the worktree exactly *sha*: abort any merge in progress, reset tracked files, drop untracked ones."""
    if merge_in_progress(wt):
        git(wt, "merge", "--abort", check=False)
    git(wt, "reset", "--hard", "--quiet", sha)
    git(wt, "clean", "-fdq")


def merge_in_progress(wt: Path) -> bool:
    return git(wt, "rev-parse", "--verify", "-q", "MERGE_HEAD", check=False).returncode == 0


def tracked_dirty(wt: Path) -> bool:
    """True when tracked files differ from HEAD (untracked files are tolerated: oracle artifacts)."""
    return bool(git(wt, "status", "--porcelain", "--untracked-files=no").stdout.strip())


def parents(repo: Path, sha: str) -> list[str]:
    return git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]


def exclude_path(repo: Path, rel: str) -> None:
    """Hide the kernel's state directory from git without touching tracked files."""
    info = Path(git(repo, "rev-parse", "--git-path", "info").stdout.strip())
    if not info.is_absolute():
        info = repo / info
    info.mkdir(parents=True, exist_ok=True)
    f = info / "exclude"
    line = f"/{rel.strip('/')}/"
    existing = f.read_text() if f.exists() else ""
    if line not in existing.splitlines():
        f.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + line + "\n")
