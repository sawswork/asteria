"""GitHub Actions エントリポイント。

3種のフォームIssueを処理する(全て同じconcurrencyグループで直列化される):
  [TURN]     ターン解決(知能層AI判断を1回のAI呼び出しに同梱。失敗はルール層)
  [GENERATE] 技生成の儀式(生成権を消費。AI生成→検証→却下ならフォールバック)
  [UPDATE]   技アップデート(3案提示→選択の2段階)

取りこぼし防止のため、イベントのIssueだけでなくオープンな対象Issueを番号順に全処理する。
各Issueの処理は「検証→適用→save/board/README書込→1コミット→push→返信→クローズ」。
pushが拒否されたらリモート先端にリセットして全体を再解決する(リプレイ方式・冪等)。
不正な入力はエラー返信+クローズのみでセーブに一切触れない。
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
from . import generation, gitops, screen, turn_ai
from .ai_client import AiClient
from .commands import ROLE_LABELS, InvalidMove, validate_commands
from .gh_api import GhApi
from .issue_parser import (
    CHOICE_VIEW,
    parse_generate_body,
    parse_issue_body,
    parse_update_body,
)
from .models import Save
from .rng import Rng
from .save_io import load_json, load_save, write_save

TITLE_TURN = "[TURN]"
TITLE_GENERATE = "[GENERATE]"
TITLE_UPDATE = "[UPDATE]"
TITLE_PREFIXES = (TITLE_TURN, TITLE_GENERATE, TITLE_UPDATE)

PROCESSED_ISSUES_MAX = 500
LABEL_PROCESSED = "turn"
MAX_PUSH_REPLAYS = 3

SAVE_DIR = "save"
BOARD_PATH = "assets/board.svg"
README_PATH = "README.md"
WORLD_PATH = "world/world.json"
BALANCE_PATH = "world/balance.json"
AI_CONFIG_PATH = "config/ai.json"


def _links(repo_slug: str) -> str:
    return (
        f"📺 [戦況ボード](https://github.com/{repo_slug}#readme) / "
        f"▶ [ターン入力](https://github.com/{repo_slug}/issues/new?template=turn.yml) / "
        f"✨ [技生成](https://github.com/{repo_slug}/issues/new?template=generate.yml) / "
        f"🔮 [技アップデート](https://github.com/{repo_slug}/issues/new?template=update.yml)"
    )


def _spell_block(spell: dict[str, Any]) -> str:
    effects = json.dumps(spell["effects"], ensure_ascii=False)
    ct = spell.get("ct", 0)
    return (
        f"**{spell['name']}**(CT{ct})\n"
        f"> {spell['desc']}\n"
        f"> `effects: {effects}`"
    )


class _Invalid(Exception):
    """入力不正: エラー返信+クローズのみ(セーブ不変・消費なし)。"""

    def __init__(self, reply_md: str) -> None:
        super().__init__(reply_md)
        self.reply_md = reply_md


def _reply_invalid_turn(errors: list[InvalidMove], repo_slug: str) -> str:
    lines = "\n".join(f"- **{e.role}**: {e.reason}" for e in errors)
    return (
        "## ⚠ 不正な手が含まれています\n\n"
        f"{lines}\n\n"
        "**ターンは消費されていません。** README のボードで CT とゲージを確認して、"
        f"[もう一度ターンを入力](https://github.com/{repo_slug}/issues/new?template=turn.yml)してください。\n"
    )


# ---- [TURN] --------------------------------------------------------------


def _handle_turn(
    save: Save,
    body: str,
    world: dict[str, Any],
    balance: dict[str, Any],
    ai: AiClient,
    repo_slug: str,
) -> tuple[Save, str, battle_mod.TurnReport]:
    started_new_battle = False
    intro_note = ""
    if save.battle is None or not save.battle.active:
        is_first = (save.stats.get("victories", 0) + save.stats.get("defeats", 0)) == 0
        if is_first:
            save = battle_mod.start_battle(save, world, balance)
        else:
            rng = Rng(save.rng_seed, save.rng_counter)
            enemy, intro, used_ai = generation.generate_enemy(save, world, balance, ai, rng)
            save.rng_counter = rng.counter
            save = battle_mod.start_battle(
                save, world, balance, enemies=[enemy], battle_name=f"{enemy.name}との戦い", intro=intro
            )
            intro_note = intro
        started_new_battle = True

    parsed = parse_issue_body(body)
    errors: list[InvalidMove] = [InvalidMove("-", e) for e in parsed.errors]
    if not errors:
        assert save.battle is not None
        errors = validate_commands(save, save.battle, parsed.commands, balance)
    if errors:
        raise _Invalid(_reply_invalid_turn(errors, repo_slug))

    overrides, flavor = turn_ai.compute_enemy_overrides(save, world, ai)
    new_save, report = battle_mod.resolve_turn(save, parsed.commands, balance, world, overrides)
    for line in flavor:
        report.lines.append(f"({line})")
        if new_save.battle:
            new_save.battle.recent_log.append(f"({line})")
            del new_save.battle.recent_log[: -battle_mod.RECENT_LOG_LIMIT]

    recruit_note = ""
    if report.result == "victory":
        every = int(balance.get("recruit_every_victories", 3))
        if every > 0 and new_save.stats.get("victories", 0) % every == 0:
            rng = Rng(new_save.rng_seed, new_save.rng_counter)
            member, _used_ai = generation.generate_recruit(new_save, world, balance, ai, rng)
            new_save.rng_counter = rng.counter
            new_save.roster_extra.append(member)
            recruit_note = (
                f"🌟 **勧誘イベント!** {member.name}({ROLE_LABELS.get(member.role, member.role)}・{member.title})"
                f"がロスターに加わった!(控え: {len(new_save.roster_extra)}人)"
            )
            report.lines.append(recruit_note)

    parts: list[str] = [f"## ⚔ ターン{report.turn}の結果\n"]
    if started_new_battle and new_save.battle:
        parts.append(f"新しい戦いが始まった: **{new_save.battle.name}**")
        if intro_note:
            parts.append(f"> {intro_note}")
        parts.append("")
    parts.append("```")
    parts.extend(report.lines)
    parts.append("```\n")
    if report.result == "victory":
        nxt = battle_mod.xp_to_next(new_save.level, balance)
        parts.append(
            f"🏆 **勝利!** Lv{new_save.level}(XP {new_save.xp}/{nxt})・技生成権 {new_save.spell_tokens}\n"
        )
        if recruit_note:
            parts.append(recruit_note + "\n")
        parts.append("次のターン送信で新しい戦いが始まります。\n")
    elif report.result == "defeat":
        parts.append("💀 **敗北……** 次のターン送信で再挑戦できます。\n")
    elif new_save.battle:
        enemy_lines = ", ".join(f"{e.name} HP {e.hp}/{e.max_hp}" for e in new_save.battle.enemies)
        parts.append(f"**敵の状態**: {enemy_lines}\n")
    if parsed.free_text:
        parts.append("> ℹ 自由記述の解釈(スロットへのマッピング)は次のマイルストーンで対応予定です。\n")
    parts.append(_links(repo_slug))
    return new_save, "\n".join(parts), report


# ---- [GENERATE] ----------------------------------------------------------


def _handle_generate(
    save: Save, body: str, world: dict[str, Any], balance: dict[str, Any], ai: AiClient, repo_slug: str
) -> tuple[Save, str]:
    parsed = parse_generate_body(body)
    if parsed.errors:
        raise _Invalid(
            "## ⚠ 入力が不正です\n\n" + "\n".join(f"- {e}" for e in parsed.errors) + f"\n\n{_links(repo_slug)}"
        )
    if save.battle is not None and save.battle.active:
        raise _Invalid(
            "## ⚠ 戦闘中は儀式を行えません\n\n今の戦いを終わらせてから技生成の儀式を行ってください。"
            f"(生成権は消費されていません)\n\n{_links(repo_slug)}"
        )
    if save.spell_tokens < 1:
        raise _Invalid(
            "## ⚠ 技生成権がありません\n\n生成権はレベルアップで1つ獲得できます。"
            f"戦いに勝利して経験値を集めてください。\n\n{_links(repo_slug)}"
        )
    import copy

    new_save = copy.deepcopy(save)
    member = new_save.member_by_role(parsed.member_role)
    assert member is not None  # ロールはパース時に検証済み
    is_ult = parsed.slot == "奥義"
    old_name = member.ultimate.name if is_ult else member.abilities[{"アビ1": 0, "アビ2": 1, "アビ3": 2}[parsed.slot]].name
    spell, used_ai = generation.generate_spell(
        new_save, world, balance, ai, member, parsed.slot, parsed.incantation, is_ult
    )
    generation.install_spell(new_save, member, parsed.slot, spell)
    new_save.spell_tokens -= 1
    pending = new_save.pending_update
    if pending and pending.get("member_role") == parsed.member_role and pending.get("slot") == parsed.slot:
        new_save.pending_update = None  # 生成でスロットが変わったら古い進化提案は無効化
    new_save.journal.append(f"{member.name}が新しい技「{spell['name']}」を紡いだ(旧「{old_name}」)")

    source_note = "" if used_ai else "\n> ⚠ AI生成が利用できなかったため、ルール層のテンプレートで代替しました。"
    reply = (
        f"## ✨ 技生成の儀式 — 完了\n\n"
        f"{member.name}の**{parsed.slot}**が「{old_name}」から生まれ変わった:\n\n"
        f"{_spell_block(spell)}\n"
        f"{source_note}\n\n"
        f"残り生成権: **{new_save.spell_tokens}**(古い技は魔導書 `save/spells/` に残ります)\n\n"
        f"{_links(repo_slug)}"
    )
    return new_save, reply


# ---- [UPDATE] ------------------------------------------------------------


def _handle_update(
    save: Save, body: str, world: dict[str, Any], balance: dict[str, Any], ai: AiClient, repo_slug: str
) -> tuple[Save, str]:
    parsed = parse_update_body(body)
    if parsed.errors:
        raise _Invalid(
            "## ⚠ 入力が不正です\n\n" + "\n".join(f"- {e}" for e in parsed.errors) + f"\n\n{_links(repo_slug)}"
        )
    if save.battle is not None and save.battle.active:
        raise _Invalid(
            f"## ⚠ 戦闘中はアップデートできません\n\n今の戦いを終わらせてから行ってください。\n\n{_links(repo_slug)}"
        )
    import copy

    new_save = copy.deepcopy(save)
    member = new_save.member_by_role(parsed.member_role)
    assert member is not None

    slot_index = {"アビ1": 0, "アビ2": 1, "アビ3": 2}
    current_obj = member.ultimate if parsed.slot == "奥義" else member.abilities[slot_index[parsed.slot]]

    if parsed.choice == CHOICE_VIEW:
        options, budget, used_ai = generation.update_spell_options(
            new_save, world, balance, ai, member, parsed.slot, parsed.direction
        )
        new_save.pending_update = {
            "member_role": parsed.member_role,
            "slot": parsed.slot,
            "spell_id": current_obj.id,  # この技に対する提案(スロットが変わったら無効)
            "options": options,
            "budget": budget,
        }
        lines = [f"## 🔮 {member.name}の{parsed.slot} — 進化方向3案(予算 {budget:.0f})\n"]
        for i, opt in enumerate(options, start=1):
            lines.append(f"### 案{i}: {opt['direction']}\n")
            lines.append(_spell_block(opt["spell"]))
            lines.append("")
        if not used_ai:
            lines.append("> ⚠ AI提案が利用できなかったため、ルール層の3案を提示しています。")
        lines.append(
            f"\n選ぶには [技アップデートフォーム](https://github.com/{repo_slug}/issues/new?template=update.yml) を"
            "もう一度開き、同じメンバー・スロットで「案1/案2/案3」を選択して送信してください。\n"
        )
        lines.append(_links(repo_slug))
        return new_save, "\n".join(lines)

    # 案1〜案3の適用
    pending = new_save.pending_update
    if (
        not pending
        or pending.get("member_role") != parsed.member_role
        or pending.get("slot") != parsed.slot
    ):
        raise _Invalid(
            "## ⚠ 選択できる提案がありません\n\n先に同じメンバー・スロットで「提案を見る」を送信して"
            f"3案を受け取ってください。\n\n{_links(repo_slug)}"
        )
    if pending.get("spell_id") and pending["spell_id"] != current_obj.id:
        raise _Invalid(
            "## ⚠ 提案が古くなっています\n\nこのスロットの技は提案の後に変わっています。"
            f"もう一度「提案を見る」から進化案を受け取ってください。\n\n{_links(repo_slug)}"
        )
    index = {"案1": 0, "案2": 1, "案3": 2}[parsed.choice]
    option = pending["options"][index]
    from .spells import spell_cost as _spell_cost  # 適用直前の最終防衛(提案が予算内であること)

    is_ult = parsed.slot == "奥義"
    if _spell_cost(int(option["spell"]["ct"]), list(option["spell"]["effects"]), balance, is_ult) > float(
        pending.get("budget", 0)
    ) + 1e-9:
        raise _Invalid(
            "## ⚠ 提案が予算を超えています\n\nもう一度「提案を見る」から進化案を受け取ってください。"
            f"\n\n{_links(repo_slug)}"
        )
    old_name = current_obj.name
    generation.apply_update_option(new_save, member, parsed.slot, option)
    new_save.pending_update = None
    new_save.journal.append(f"{member.name}の「{old_name}」が「{option['spell']['name']}」へ進化した")
    reply = (
        f"## 🔮 技アップデート — 完了({parsed.choice}: {option['direction']})\n\n"
        f"{member.name}の**{parsed.slot}**「{old_name}」が進化した:\n\n"
        f"{_spell_block(option['spell'])}\n\n"
        f"(使い込み回数・撃破数は引き継がれます)\n\n{_links(repo_slug)}"
    )
    return new_save, reply


# ---- 共通処理 ------------------------------------------------------------


def process_issue(
    issue: dict[str, Any],
    repo_slug: str,
    root: str,
    do_git: bool,
    gh: GhApi | None,
    ai: AiClient | None = None,
) -> None:
    number = int(issue["number"])
    title = str(issue.get("title", ""))
    body = str(issue.get("body") or "")
    author = str(issue.get("user", {}).get("login", ""))
    owner = repo_slug.split("/")[0]

    if not title.startswith(TITLE_PREFIXES):
        print(f"skip #{number}: title has no known prefix")
        return
    if author != owner:
        # 公開運用時の防御: 他者の投稿は処理しない(ワークフロー側のifと二重チェック)
        print(f"skip #{number}: issue author is not the repository owner")
        return

    root_path = Path(root)
    world = load_json(root_path / WORLD_PATH)
    balance = load_json(root_path / BALANCE_PATH)
    if ai is None:
        ai = AiClient(config_path=root_path / AI_CONFIG_PATH)

    last_error = ""
    for attempt in range(MAX_PUSH_REPLAYS):
        save = load_save(root_path / SAVE_DIR)

        if number in save.processed_issues:
            if gh:
                gh.post_comment(number, "ℹ このIssueは処理済みです(セーブは変更されていません)。")
                gh.close_issue(number)
            print(f"skip #{number}: already processed")
            return

        try:
            if title.startswith(TITLE_GENERATE):
                new_save, reply = _handle_generate(save, body, world, balance, ai, repo_slug)
            elif title.startswith(TITLE_UPDATE):
                new_save, reply = _handle_update(save, body, world, balance, ai, repo_slug)
            else:
                new_save, reply, _report = _handle_turn(save, body, world, balance, ai, repo_slug)
        except _Invalid as e:
            if gh:
                gh.post_comment(number, e.reply_md)
                gh.close_issue(number)
            print(f"invalid input on issue #{number}; nothing consumed")
            return

        new_save.processed_issues.append(number)
        del new_save.processed_issues[:-PROCESSED_ISSUES_MAX]

        write_save(new_save, root_path / SAVE_DIR)
        svg = board_mod.build_board_svg(new_save, world, balance)
        board_file = root_path / BOARD_PATH
        board_file.parent.mkdir(parents=True, exist_ok=True)
        board_file.write_text(svg, encoding="utf-8")
        cache_key = f"i{number}-a{attempt}"  # Issue番号で一意(camoキャッシュ回避)
        (root_path / README_PATH).write_text(
            screen.render_readme(new_save, world, repo_slug, cache_key), encoding="utf-8"
        )

        pushed = True
        if do_git:
            gitops.configure_identity(root)
            gitops.commit([SAVE_DIR, BOARD_PATH, README_PATH], f"apply issue #{number}", cwd=root)
            pushed, last_error = gitops.push_once(cwd=root)

        if pushed:
            if gh:
                gh.add_labels(number, [LABEL_PROCESSED])
                gh.post_comment(number, reply)
                gh.close_issue(number)
            print(f"issue #{number} applied")
            return

        # push拒否: リモート先端に合わせて全体をリプレイ(セーブもリモートの物に戻る)
        print(f"push rejected for issue #{number} (attempt {attempt + 1}); replaying from remote state")
        gitops.sync_with_remote(cwd=root)
        time.sleep(2**attempt)

    raise RuntimeError(f"push failed after {MAX_PUSH_REPLAYS} replays: {last_error}")


def _collect_issues(event: dict[str, Any], gh: GhApi | None) -> list[dict[str, Any]]:
    """イベントのIssue+オープンな対象Issueを番号昇順で返す(取りこぼし回収)。"""
    by_number: dict[int, dict[str, Any]] = {}
    if gh:
        try:
            for it in gh.list_open_turn_issues(TITLE_PREFIXES):
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
    parser.add_argument("--mock", action="store_true", help="AI応答をfixtures/aiの固定JSONにする")
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

    ai = AiClient(
        mock=args.mock,
        fixtures_dir=Path(args.root) / "fixtures/ai",
        config_path=Path(args.root) / AI_CONFIG_PATH,
    )

    for issue in _collect_issues(event, gh):
        try:
            process_issue(issue, args.repo, args.root, not args.no_git, gh, ai)
        except Exception as e:  # エラーは該当Issueに要約だけ返して失敗させる(全文やSecretsは出さない)
            if gh:
                try:
                    gh.post_comment(
                        int(issue["number"]),
                        "## 💥 エンジンエラー\n\n処理中に問題が発生しました。"
                        f"セーブは直前の状態のままです。\n\n`{type(e).__name__}`",
                    )
                except Exception:
                    pass
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
