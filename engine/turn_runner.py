"""GitHub Actions エントリポイント。

issuesイベント(ターン入力フォーム)を受けて1ターンを処理する:
  検証 → 戦闘解決 → セーブ/ボード書込 → コミット(SHA取得) → README更新 → push → 結果返信 → クローズ

不正手はエラー返信+クローズのみでセーブに一切触れない(ターン不消費)。
処理済みIssueの再実行は冪等(セーブ不変のまま案内コメントを返す)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import battle as battle_mod
from . import board as board_mod
from . import gitops
from . import screen
from .commands import InvalidMove, validate_commands
from .gh_api import GhApi
from .issue_parser import parse_issue_body
from .models import Save
from .save_io import load_json, load_save, write_save

TITLE_PREFIX = "[TURN]"
PROCESSED_ISSUES_MAX = 500
LABEL_PROCESSED = "turn"

SAVE_PATH = "save/state.json"
BOARD_PATH = "assets/board.svg"
README_PATH = "README.md"
WORLD_PATH = "world/world.json"
BALANCE_PATH = "world/balance.json"


def _issue_url(repo_slug: str, number: int) -> str:
    return f"https://github.com/{repo_slug}/issues/{number}"


def _reply_invalid(errors: list[InvalidMove], repo_slug: str) -> str:
    lines = "\n".join(f"- **{e.role}**: {e.reason}" for e in errors)
    return (
        "## ⚠ 不正な手が含まれています\n\n"
        f"{lines}\n\n"
        "**ターンは消費されていません。** README のボードで CT とゲージを確認して、"
        f"[もう一度ターンを入力](https://github.com/{repo_slug}/issues/new?template=turn.yml)してください。\n"
    )


def _reply_result(
    report: battle_mod.TurnReport, save: Save, repo_slug: str, started_new_battle: bool, free_text: str
) -> str:
    parts: list[str] = []
    parts.append(f"## ⚔ ターン{report.turn}の結果\n")
    if started_new_battle and save.battle:
        parts.append(f"新しい戦いが始まった: **{save.battle.name}**\n")
    parts.append("```")
    parts.extend(report.lines)
    parts.append("```\n")
    if report.result == "victory":
        parts.append("🏆 **勝利!** 次のターン送信で新しい戦いが始まります。\n")
    elif report.result == "defeat":
        parts.append("💀 **敗北……** 次のターン送信で再挑戦できます。\n")
    elif save.battle:
        enemy_lines = ", ".join(f"{e.name} HP {e.hp}/{e.max_hp}" for e in save.battle.enemies)
        parts.append(f"**敵の状態**: {enemy_lines}\n")
    if free_text:
        parts.append("> ℹ 自由記述欄はM2(生成系)で対応予定です。今回はスロット選択のみ実行しました。\n")
    parts.append(
        f"📺 [最新の戦況ボードを見る](https://github.com/{repo_slug}#readme) / "
        f"▶ [次のターンを入力する](https://github.com/{repo_slug}/issues/new?template=turn.yml)"
    )
    return "\n".join(parts)


def process_issue_event(event: dict[str, Any], repo_slug: str, root: str, do_git: bool, gh: GhApi | None) -> int:
    issue = event["issue"]
    number = int(issue["number"])
    title = str(issue.get("title", ""))
    body = str(issue.get("body") or "")
    author = str(issue.get("user", {}).get("login", ""))
    owner = repo_slug.split("/")[0]

    if not title.startswith(TITLE_PREFIX):
        print(f"skip: title does not start with {TITLE_PREFIX}")
        return 0
    if author != owner:
        # 公開運用時の防御: 他者のターン投稿は処理しない(ワークフロー側のifと二重チェック)
        print("skip: issue author is not the repository owner")
        return 0

    root_path = Path(root)
    world = load_json(root_path / WORLD_PATH)
    balance = load_json(root_path / BALANCE_PATH)
    save = load_save(root_path / SAVE_PATH)

    if number in save.processed_issues:
        if gh:
            gh.post_comment(number, "ℹ このターンは処理済みです(セーブは変更されていません)。")
            gh.close_issue(number)
        print(f"skip: issue #{number} already processed")
        return 0

    started_new_battle = False
    if save.battle is None or not save.battle.active:
        save = battle_mod.start_battle(save, world, balance)
        started_new_battle = True

    parsed = parse_issue_body(body)
    ult_max = int(balance["ult_gauge"]["max"])
    errors: list[InvalidMove] = [InvalidMove("-", e) for e in parsed.errors]
    if not errors:
        assert save.battle is not None
        errors = validate_commands(save, save.battle, parsed.commands, ult_max)
    if errors:
        if gh:
            gh.post_comment(number, _reply_invalid(errors, repo_slug))
            gh.close_issue(number)
        print(f"invalid move(s) on issue #{number}; turn not consumed")
        return 0

    new_save, report = battle_mod.resolve_turn(save, parsed.commands, balance)
    new_save.processed_issues.append(number)
    del new_save.processed_issues[:-PROCESSED_ISSUES_MAX]

    write_save(new_save, root_path / SAVE_PATH)
    svg = board_mod.build_board_svg(new_save, world, balance)
    board_file = root_path / BOARD_PATH
    board_file.parent.mkdir(parents=True, exist_ok=True)
    board_file.write_text(svg, encoding="utf-8")

    sha = "local"
    if do_git:
        gitops.configure_identity(root)
        sha = gitops.commit([SAVE_PATH, BOARD_PATH], f"turn {report.turn}: issue #{number}", cwd=root)
    (root_path / README_PATH).write_text(
        screen.render_readme(new_save, world, repo_slug, sha), encoding="utf-8"
    )
    if do_git:
        gitops.commit([README_PATH], f"screen: update board for turn {report.turn}", cwd=root)
        gitops.push(cwd=root)

    if gh:
        gh.add_labels(number, [LABEL_PROCESSED])
        gh.post_comment(number, _reply_result(report, new_save, repo_slug, started_new_battle, parsed.free_text))
        gh.close_issue(number)
    print(f"turn {report.turn} resolved for issue #{number} (result={report.result})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Actionsターン処理エントリポイント")
    parser.add_argument("--event-path", required=True, help="GITHUB_EVENT_PATH のJSON")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--root", default=".", help="リポジトリルート")
    parser.add_argument("--no-git", action="store_true", help="コミット/pushしない(テスト用)")
    parser.add_argument("--no-github", action="store_true", help="コメント/クローズしない(テスト用)")
    args = parser.parse_args(argv)

    if not args.repo:
        print("error: --repo または GITHUB_REPOSITORY が必要です", file=sys.stderr)
        return 2
    with open(args.event_path, encoding="utf-8") as f:
        event = json.load(f)
    if "issue" not in event:
        print("skip: not an issue event")
        return 0

    gh: GhApi | None = None
    if not args.no_github:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            print("error: GITHUB_TOKEN が必要です", file=sys.stderr)
            return 2
        gh = GhApi(args.repo, token)
    try:
        return process_issue_event(event, args.repo, args.root, not args.no_git, gh)
    except Exception as e:  # エラーはIssueに要約だけ返して失敗させる(全文やSecretsは出さない)
        if gh:
            try:
                number = int(event["issue"]["number"])
                gh.post_comment(
                    number,
                    "## 💥 エンジンエラー\n\nターン処理中に問題が発生しました。"
                    f"セーブは直前の状態のままです。\n\n`{type(e).__name__}`",
                )
            except Exception:
                pass
        raise


if __name__ == "__main__":
    sys.exit(main())
