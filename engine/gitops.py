"""git 操作境界。Actions内でセーブ・ボード・READMEをコミットしてpushする。"""
from __future__ import annotations

import subprocess
import time

BOT_NAME = "asteria-engine[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
PUSH_RETRIES = 4


def _git(*args: str, cwd: str = ".") -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def configure_identity(cwd: str = ".") -> None:
    _git("config", "user.name", BOT_NAME, cwd=cwd)
    _git("config", "user.email", BOT_EMAIL, cwd=cwd)


def commit(paths: list[str], message: str, cwd: str = ".") -> str:
    """paths をステージしてコミットし、コミットSHAを返す。差分ゼロなら現HEADを返す。"""
    _git("add", *paths, cwd=cwd)
    status = _git("status", "--porcelain", "--untracked-files=no", cwd=cwd)
    if not any(line and line[0] != " " for line in status.splitlines()):
        return _git("rev-parse", "HEAD", cwd=cwd)
    _git("commit", "-m", message, cwd=cwd)
    return _git("rev-parse", "HEAD", cwd=cwd)


def push(cwd: str = ".") -> None:
    """push。競合したら pull --rebase して指数バックオフで再試行する。"""
    last: Exception | None = None
    for attempt in range(PUSH_RETRIES):
        try:
            _git("push", cwd=cwd)
            return
        except subprocess.CalledProcessError as e:
            last = e
            time.sleep(2**attempt)
            try:
                _git("pull", "--rebase", cwd=cwd)
            except subprocess.CalledProcessError:
                pass
    raise RuntimeError(f"git push failed after {PUSH_RETRIES} attempts: {last}")
