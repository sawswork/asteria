"""GitHub Actions エントリポイント。

issuesイベント(ターン入力フォーム)を受けてターンを処理する。取りこぼし防止のため、
イベントのIssueだけでなくオープンな [TURN] Issue を番号順に全て処理する
(concurrencyのpendingスロットは1つしかなく、連投時に待機中のrunがキャンセルされ得るため)。

各Issueの処理:
  検証 → 戦闘解決 → セーブ/ボード/README書込 → 1コミット → push → 結果返信 → クローズ

push が拒否された場合(直列化の外からリポジトリが進んだ場合)は、リモート先端に
リセットしてからターン全体を再解決する(リプレイ方式。処理済みIssueは冪等スキップ)。
不正手はエラー返信+クローズのみでセーブに一切触れない(ターン不消費)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
MAX_PUSH_REPLAYS = 3

SAVE_DIR = "save"
BOARD_PATH = "assets/board.svg"
README_PATH = "README.md"
WORLD_PATH = "world/world.json"
BALANCE_PATH = "world/balance.json"


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


def process_issue(issue: dict[str, Any], repo_slug: str, root: str, do_git: bool, gh: GhApi | None) -> None:
    number = int(issue["number"])
    title = str(issue.get("title", ""))
    body = str(issue.get("body") or "")
    author = str(issue.get("user", {}).get("login", ""))
    owner = repo_slug.split("/")[0]

    if not title.startswith(TITLE_PREFIX):
        print(f"skip #{number}: title does not start with {TITLE_PREFIX}")
        return
    if author != owner:
        # 公開運用時の防御: 他者のターン投稿は処理しない(ワークフロー側のifと二重チェック)
        print(f"skip #{number}: issue author is not the repository owner")
        return

    root_path = Path(root)
    world = load_json(root_path / WORLD_PATH)
    balance = load_json(root_path / BALANCE_PATH)

    last_error = ""
    for attempt in range(MAX_PUSH_REPLAYS):
        save = load_save(root_path / SAVE_DIR)

        if number in save.processed_issues:
            if gh:
                gh.post_comment(number, "ℹ このターンは処理済みです(セーブは変更されていません)。")
                gh.close_issue(number)
            print(f"skip #{number}: already processed")
            return

        started_new_battle = False
        if save.battle is None or not save.battle.active:
            save = battle_mod.start_battle(save, world, balance)
            started_new_battle = True

        parsed = parse_issue_body(body)
        errors: list[InvalidMove] = [InvalidMove("-", e) for e in parsed.errors]
        if not errors:
            assert save.battle is not None
            errors = validate_commands(save, save.battle, parsed.commands, balance)
        if errors:
            if gh:
                gh.post_comment(number, _reply_invalid(errors, repo_slug))
                gh.close_issue(number)
            print(f"invalid move(s) on issue #{number}; turn not consumed")
            return

        new_save, report = battle_mod.resolve_turn(save, parsed.commands, balance, world)
        new_save.processed_issues.append(number)
        del new_save.processed_issues[:-PROCESSED_ISSUES_MAX]

        write_save(new_save, root_path / SAVE_DIR)
        svg = board_mod.build_board_svg(new_save, world, balance)
        board_file = root_path / BOARD_PATH
        board_file.parent.mkdir(parents=True, exist_ok=True)
        board_file.write_text(svg, encoding="utf-8")
        cache_key = f"t{report.turn}-i{number}"  # ターン×Issue番号で一意(camoキャッシュ回避)
        (root_path / README_PATH).write_text(
            screen.render_readme(new_save, world, repo_slug, cache_key), encoding="utf-8"
        )

        pushed = True
        if do_git:
            gitops.configure_identity(root)
            gitops.commit(
                [SAVE_DIR, BOARD_PATH, README_PATH],
                f"turn {report.turn}: issue #{number}",
                cwd=root,
            )
            pushed, last_error = gitops.push_once(cwd=root)

        if pushed:
            if gh:
                gh.add_labels(number, [LABEL_PROCESSED])
                gh.post_comment(
                    number, _reply_result(report, new_save, repo_slug, started_new_battle, parsed.free_text)
                )
                gh.close_issue(number)
            print(f"turn {report.turn} resolved for issue #{number} (result={report.result})")
            return

        # push拒否: リモート先端に合わせて全体をリプレイ(セーブもリモートの物に戻る)
        print(f"push rejected for issue #{number} (attempt {attempt + 1}); replaying from remote state")
        gitops.sync_with_remote(cwd=root)
        time.sleep(2**attempt)

    raise RuntimeError(f"turn push failed after {MAX_PUSH_REPLAYS} replays: {last_error}")


def _collect_issues(event: dict[str, Any], gh: GhApi | None) -> list[dict[str, Any]]:
    """イベントのIssue+オープンな[TURN] Issueを番号昇順で返す(取りこぼし回収)。"""
    by_number: dict[int, dict[str, Any]] = {}
    if gh:
        try:
            for it in gh.list_open_turn_issues(TITLE_PREFIX):
                by_number[int(it["number"])] = it
        except RuntimeError as e:
            print(f"warning: could not list open issues ({e}); processing event issue only")
    ev = event["issue"]
    by_number.setdefault(int(ev["number"]), ev)
    return [by_number[n] for n in sorted(by_number)]


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

    for issue in _collect_issues(event, gh):
        try:
            process_issue(issue, args.repo, args.root, not args.no_git, gh)
        except Exception as e:  # エラーは該当Issueに要約だけ返して失敗させる(全文やSecretsは出さない)
            if gh:
                try:
                    gh.post_comment(
                        int(issue["number"]),
                        "## 💥 エンジンエラー\n\nターン処理中に問題が発生しました。"
                        f"セーブは直前の状態のままです。\n\n`{type(e).__name__}`",
                    )
                except Exception:
                    pass
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
