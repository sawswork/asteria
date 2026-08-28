"""GitHub REST API 境界。GITHUB_TOKEN で認証し、コメント投稿・クローズ・ラベル付与を行う。

トークンやレスポンス全文はログに出さない。失敗はリトライ(指数バックオフ)して最後は例外。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

API_BASE = "https://api.github.com"
RETRIES = 3


class GhApi:
    def __init__(self, repo_slug: str, token: str) -> None:
        self.repo_slug = repo_slug
        self._token = token

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{API_BASE}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        last_error: Exception | None = None
        for attempt in range(RETRIES):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self._token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode()
                    return json.loads(body) if body else None
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
                last_error = e
                if isinstance(e, urllib.error.HTTPError) and e.code < 500 and e.code != 429:
                    break  # 4xx はリトライしない(429以外)
                time.sleep(2**attempt)
        raise RuntimeError(f"GitHub API {method} {path} failed: {last_error}")

    def list_open_turn_issues(self, title_prefix: str | tuple[str, ...]) -> list[dict[str, Any]]:
        """オープンなターンIssueを番号昇順で返す(PRは除外)。取りこぼしIssueの回収に使う。"""
        issues: list[dict[str, Any]] = []
        for page in range(1, 4):
            batch = self._request(
                "GET",
                f"/repos/{self.repo_slug}/issues"
                f"?state=open&sort=created&direction=asc&per_page=100&page={page}",
            )
            if not batch:
                break
            for it in batch:
                if "pull_request" in it:
                    continue
                if str(it.get("title", "")).startswith(title_prefix):
                    issues.append(it)
            if len(batch) < 100:
                break
        issues.sort(key=lambda i: int(i["number"]))
        return issues

    def list_game_issues(self, title_prefix: str | tuple[str, ...]) -> list[dict[str, Any]]:
        """開閉問わず対象Issueを番号昇順で返す(年代記の復元に使う)。"""
        issues: list[dict[str, Any]] = []
        for page in range(1, 11):
            batch = self._request(
                "GET",
                f"/repos/{self.repo_slug}/issues"
                f"?state=all&sort=created&direction=asc&per_page=100&page={page}",
            )
            if not batch:
                break
            for it in batch:
                if "pull_request" in it:
                    continue
                if str(it.get("title", "")).startswith(title_prefix):
                    issues.append(it)
            if len(batch) < 100:
                break
        issues.sort(key=lambda i: int(i["number"]))
        return issues

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        got = self._request(
            "GET", f"/repos/{self.repo_slug}/issues/{issue_number}/comments?per_page=100"
        )
        return list(got) if isinstance(got, list) else []

    def post_comment(self, issue_number: int, body: str) -> None:
        self._request(
            "POST", f"/repos/{self.repo_slug}/issues/{issue_number}/comments", {"body": body}
        )

    def close_issue(self, issue_number: int) -> None:
        self._request(
            "PATCH",
            f"/repos/{self.repo_slug}/issues/{issue_number}",
            {"state": "closed", "state_reason": "completed"},
        )

    # ---- PR攻撃(実PRの作成・監視・強制マージ) ----------------------------

    def get_branch_sha(self, branch: str) -> str:
        ref = self._request("GET", f"/repos/{self.repo_slug}/git/ref/heads/{branch}")
        return str(ref["object"]["sha"])

    def create_branch(self, name: str, sha: str) -> None:
        self._request(
            "POST", f"/repos/{self.repo_slug}/git/refs", {"ref": f"refs/heads/{name}", "sha": sha}
        )

    def put_file(self, path: str, text: str, message: str, branch: str) -> None:
        import base64

        content = base64.b64encode(text.encode("utf-8")).decode("ascii")
        self._request(
            "PUT",
            f"/repos/{self.repo_slug}/contents/{path}",
            {"message": message, "content": content, "branch": branch},
        )

    def create_pull(self, title: str, body: str, head: str, base: str) -> int:
        pr = self._request(
            "POST",
            f"/repos/{self.repo_slug}/pulls",
            {"title": title, "body": body, "head": head, "base": base},
        )
        return int(pr["number"])

    def find_open_pull_by_head(self, branch: str) -> int:
        """headブランチが branch のオープンPR番号(無ければ0)。リプレイでの二重作成を防ぐ。"""
        owner = self.repo_slug.split("/")[0]
        pulls = self._request(
            "GET", f"/repos/{self.repo_slug}/pulls?state=open&head={owner}:{branch}&per_page=1"
        )
        if isinstance(pulls, list) and pulls:
            return int(pulls[0]["number"])
        return 0

    def branch_exists(self, name: str) -> bool:
        try:
            self._request("GET", f"/repos/{self.repo_slug}/git/ref/heads/{name}")
            return True
        except RuntimeError:
            return False

    def get_pull(self, number: int) -> dict[str, Any]:
        """PRの状態と素性(作者・headブランチ・headSHA)を返す。"""
        pr = self._request("GET", f"/repos/{self.repo_slug}/pulls/{number}")
        return {
            "state": str(pr.get("state", "unknown")),
            "merged": bool(pr.get("merged", False)),
            "author": str((pr.get("user") or {}).get("login", "")),
            "head_ref": str((pr.get("head") or {}).get("ref", "")),
            "head_sha": str((pr.get("head") or {}).get("sha", "")),
        }

    def pull_changed_files(self, number: int) -> list[str]:
        files = self._request("GET", f"/repos/{self.repo_slug}/pulls/{number}/files?per_page=100")
        return [str(f["filename"]) for f in files] if isinstance(files, list) else []

    def merge_pull(self, number: int, title: str) -> bool:
        try:
            self._request(
                "PUT",
                f"/repos/{self.repo_slug}/pulls/{number}/merge",
                {"merge_method": "merge", "commit_title": title},
            )
            return True
        except RuntimeError:
            return False

    def close_pull(self, number: int) -> None:
        self._request("PATCH", f"/repos/{self.repo_slug}/pulls/{number}", {"state": "closed"})

    def delete_branch(self, name: str) -> None:
        try:
            self._request("DELETE", f"/repos/{self.repo_slug}/git/refs/heads/{name}")
        except RuntimeError:
            pass  # 掃除は失敗しても続行

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        try:
            self._request(
                "POST",
                f"/repos/{self.repo_slug}/issues/{issue_number}/labels",
                {"labels": labels},
            )
        except RuntimeError:
            pass  # ラベルは装飾。未定義などで失敗してもターン処理は続行する
