"""git 操作境界。Actions内でセーブ・ボード・READMEをコミットしてpushする。

push が拒否された場合は rebase せず、呼び出し側(turn_runner)が sync_with_remote で
リモートの最新状態にリセットしてからターン全体を再解決する(リプレイ方式)。
"""
from __future__ import annotations

import subprocess

BOT_NAME = "rpg-engine[bot]"  # 世界に依存しない中立の実行者名(不変則: エンジンに固有名詞を書かない)
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class GitError(RuntimeError):
    pass


def _git(*args: str, cwd: str = ".") -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        # stderr要約を含める(checkoutの認証はextraheader方式なのでトークンは混入しない)
        raise GitError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def configure_identity(cwd: str = ".") -> None:
    _git("config", "user.name", BOT_NAME, cwd=cwd)
    _git("config", "user.email", BOT_EMAIL, cwd=cwd)


def current_branch(cwd: str = ".") -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)


def commit(paths: list[str], message: str, cwd: str = ".") -> str:
    """paths をステージしてコミットし、コミットSHAを返す。差分ゼロなら現HEADを返す。"""
    _git("add", *paths, cwd=cwd)
    status = _git("status", "--porcelain", "--untracked-files=no", cwd=cwd)
    if not any(line and line[0] != " " for line in status.splitlines()):
        return _git("rev-parse", "HEAD", cwd=cwd)
    _git("commit", "-m", message, cwd=cwd)
    return _git("rev-parse", "HEAD", cwd=cwd)


def push_once(cwd: str = ".") -> tuple[bool, str]:
    """1回だけpushを試みる。失敗時は (False, stderr要約) を返す。"""
    result = subprocess.run(["git", "push"], cwd=cwd, capture_output=True, text=True)
    if result.returncode == 0:
        return True, ""
    return False, result.stderr.strip()[:500]


def sync_with_remote(cwd: str = ".") -> None:
    """途中状態を破棄してリモートの最新ブランチ先端に完全一致させる。"""
    subprocess.run(["git", "rebase", "--abort"], cwd=cwd, capture_output=True, text=True)
    branch = current_branch(cwd)
    _git("fetch", "origin", branch, cwd=cwd)
    _git("reset", "--hard", f"origin/{branch}", cwd=cwd)
