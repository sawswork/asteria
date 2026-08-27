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

    def list_open_turn_issues(self, title_prefix: str) -> list[dict[str, Any]]:
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

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        try:
            self._request(
                "POST",
                f"/repos/{self.repo_slug}/issues/{issue_number}/labels",
                {"labels": labels},
            )
        except RuntimeError:
            pass  # ラベルは装飾。未定義などで失敗してもターン処理は続行する
