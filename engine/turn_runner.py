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
import re
import sys
import time
from pathlib import Path
from typing import Any

from . import battle as battle_mod
from . import board as board_mod
from . import generation, gitops, screen, turn_ai
from .ai_client import AiClient
from .commands import (
    ABILITY_INDEX,
    ACTION_NORMAL,
    ACTION_ULT,
    ACTION_WAIT,
    ROLE_LABELS,
    TARGET_AUTO,
    Command,
    InvalidMove,
    validate_commands,
)
from .spells import constraint_multiplier, known_constraints
from .gh_api import GhApi
from .issue_parser import (
    CHOICE_VIEW,
    parse_generate_body,
    parse_issue_body,
    parse_rewind_body,
    parse_update_body,
)
from .models import Save
from .rng import Rng
from .save_io import load_json, load_save, write_save

TITLE_TURN = "[TURN]"
TITLE_GENERATE = "[GENERATE]"
TITLE_UPDATE = "[UPDATE]"
TITLE_REWIND = "[REWIND]"
TITLE_PREFIXES = (TITLE_TURN, TITLE_GENERATE, TITLE_UPDATE, TITLE_REWIND)

PROCESSED_ISSUES_MAX = 500
LABEL_PROCESSED = "turn"
MAX_PUSH_REPLAYS = 3

SAVE_DIR = "save"
ASSETS_DIR = "assets"
BOARD_PATH = "assets/board.svg"
SCENE_PATH = "assets/scene.svg"
README_PATH = "README.md"
WORLD_PATH = "world/world.json"
BALANCE_PATH = "world/balance.json"
AI_CONFIG_PATH = "config/ai.json"


GENERATED_MARKER = "assets/raw/.generated.json"  # Gemini生成素材の由来(敵ID)を記録
OVERRIDE_PATH = "battle_override.json"  # PR攻撃で適用される戦闘スコープのバランス上書き


def _merged_balance(root_path: Path) -> dict[str, Any]:
    """balance.json に battle_override.json(存在時のみ)を深マージして返す。

    overrideはPR攻撃のマージでのみ出現し、戦闘終了時に撤去される(数値の出所は常にリポジトリ内)。
    """
    balance = load_json(root_path / BALANCE_PATH)
    override_file = root_path / OVERRIDE_PATH
    if not override_file.exists():
        return balance
    try:
        data = load_json(override_file)
    except (OSError, json.JSONDecodeError):
        return balance

    def deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                deep_merge(dst[k], v)
            else:
                dst[k] = v

    import copy

    merged = copy.deepcopy(balance)
    deep_merge(merged, dict(data.get("overrides", {})))
    return merged


def _maybe_generate_materials(root_path: Path, save: Save, world: dict[str, Any]) -> None:
    """Gemini素材の生成判断。ユーザーが置いた素材(マーカー無し)は決して上書きしない。"""
    from . import assets as assets_mod
    from . import gemini as gemini_mod

    enemy = save.battle.enemies[0] if save.battle and save.battle.enemies else None
    if enemy is None:
        return
    client = gemini_mod.GeminiClient()
    if not client.available():
        return
    raw_dir = root_path / assets_mod.RAW_DIR
    marker = root_path / GENERATED_MARKER
    if assets_mod.has_raw_assets(root_path):
        if not marker.exists():
            return  # ユーザー素材が優先(自動生成で置き換えない)
        try:
            if json.loads(marker.read_text(encoding="utf-8")).get("enemy_id") == enemy.id:
                return  # 同じ敵の生成済み素材を再利用
        except (OSError, json.JSONDecodeError):
            pass
        # 敵が変わった: 旧AI生成素材を消して作り直す
        for f in raw_dir.iterdir():
            if f.suffix.lower() in assets_mod.RAW_EXTS:
                f.unlink()
    if client.generate_enemy_assets(enemy, world, raw_dir) > 0:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"enemy_id": enemy.id}, ensure_ascii=False), encoding="utf-8")


def prepare_scene(
    root_path: Path, save: Save, world: dict[str, Any], allow_generation: bool = True
) -> bool:
    """新しい戦闘のシーンSVGを生成して書き込む(素材→(任意)Gemini→プレースホルダ)。

    どこかで失敗してもゲームは止めない(古いシーンは消してボードのみで続行)。
    """
    try:
        from . import assets as assets_mod
        from . import scene as scene_mod

        if allow_generation:
            try:
                _maybe_generate_materials(root_path, save, world)
            except Exception as e:
                print(f"gemini: material generation failed ({type(e).__name__}); continuing")
        if assets_mod.has_raw_assets(root_path):
            try:
                assets_mod.process_raw_assets(root_path)
            except Exception as e:
                print(f"assets: pipeline failed ({type(e).__name__}); using placeholder scene")
        svg = scene_mod.build_scene_svg(save, world, str(root_path))
        scene_file = root_path / SCENE_PATH
        scene_file.parent.mkdir(parents=True, exist_ok=True)
        scene_file.write_text(svg, encoding="utf-8")
        return True
    except Exception as e:
        print(f"scene: generation failed ({type(e).__name__}); board only")
        (root_path / SCENE_PATH).unlink(missing_ok=True)  # 前の戦闘のシーンを残さない
        return False


def _links(repo_slug: str) -> str:
    return (
        f"📺 [戦況ボード](https://github.com/{repo_slug}#readme) / "
        f"▶ [ターン入力](https://github.com/{repo_slug}/issues/new?template=turn.yml) / "
        f"✨ [技生成](https://github.com/{repo_slug}/issues/new?template=generate.yml) / "
        f"🔮 [技アップデート](https://github.com/{repo_slug}/issues/new?template=update.yml) / "
        f"⏪ [時戻し](https://github.com/{repo_slug}/issues/new?template=rewind.yml)"
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

_FULL_AUTO_RE = re.compile(r"フルオート\s*(\d+)")  # 自由記述「フルオート N」= 合計Nターンまで自動続行


def _evolution_overrides(
    save: Save, world: dict[str, Any], balance: dict[str, Any], ai: AiClient
) -> dict[str, dict[str, Any]]:
    """進化予告済みの敵の演出をAIに生成させる(生成層で検証済み。失敗は決定的演出)。"""
    battle = save.battle
    if battle is None or not battle.active:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for e in battle.enemies:
        if e.alive and e.evolution_pending is not None:
            spec, _used_ai = generation.generate_evolution(save, world, balance, ai, e)
            out[e.id] = spec
    return out


def _auto_commands(save: Save, balance: dict[str, Any]) -> dict[str, Command]:
    """フルオートの自動採択(決定的ルール)。奥義解放>回復(必要時)>攻撃アビ>通常攻撃。

    誓約付きの技は条件外で不正手になり得るため自動では使わない。
    """
    ult_max = int(balance["ult_gauge"]["max"])
    cmds: dict[str, Command] = {}
    need_heal = any(m.alive and m.hp < m.max_hp * 0.6 for m in save.party)
    for m in save.party:
        if not m.alive:
            cmds[m.role] = Command(role=m.role, action=ACTION_WAIT, target=TARGET_AUTO)
            continue
        action = ACTION_NORMAL
        if m.ult_gauge >= ult_max and not m.ultimate.constraints:
            action = ACTION_ULT
        else:
            for label, idx in ABILITY_INDEX.items():
                a = m.abilities[idx]
                if a.ready_in > 0 or a.constraints:
                    continue
                tags = {e.get("tag") for e in a.effects}
                if need_heal and "heal" in tags:
                    action = label
                    break
                if not need_heal and ("damage" in tags or "dot" in tags):
                    action = label
                    break
        cmds[m.role] = Command(role=m.role, action=action, target=TARGET_AUTO)
    return cmds


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
        nemesis = battle_mod.nemesis_enemy(save)
        if is_first:
            save = battle_mod.start_battle(save, world, balance)
        elif nemesis is not None:
            # 宿敵は生成をスキップして必ず再登場する(撃破されるまで新しい敵は現れない)
            enemy, battle_name, intro = nemesis
            save = battle_mod.start_battle(
                save, world, balance, enemies=[enemy], battle_name=battle_name, intro=intro
            )
            intro_note = intro
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

    def _run_one(cur: Save, commands: dict[str, Command]) -> tuple[Save, battle_mod.TurnReport]:
        overrides, flavor = turn_ai.compute_enemy_overrides(cur, world, ai)
        evo = _evolution_overrides(cur, world, balance, ai)
        nxt, rep = battle_mod.resolve_turn(cur, commands, balance, world, overrides, evo)
        for line in flavor:
            rep.lines.append(f"({line})")
            if nxt.battle:
                nxt.battle.recent_log.append(f"({line})")
                del nxt.battle.recent_log[: -battle_mod.RECENT_LOG_LIMIT]
        return nxt, rep

    new_save, report = _run_one(save, parsed.commands)
    all_lines = list(report.lines)
    first_turn = report.turn
    auto_note = ""
    auto_match = _FULL_AUTO_RE.search(parsed.free_text)
    if auto_match:
        limit = max(1, min(int(auto_match.group(1)), int(balance.get("full_auto_max_turns", 8))))
        turns_done = 1
        while turns_done < limit and not report.result and new_save.battle and new_save.battle.active:
            auto_cmds = _auto_commands(new_save, balance)
            if validate_commands(new_save, new_save.battle, auto_cmds, balance):
                break  # 自動手が組めない状態(想定外)。ここまでの結果で打ち切る
            new_save, report = _run_one(new_save, auto_cmds)
            all_lines.extend(report.lines)
            turns_done += 1
        auto_note = f"🤖 フルオート: {turns_done}ターンを自動解決(指定{limit}ターン上限)"

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

    turn_range = f"ターン{first_turn}" if report.turn == first_turn else f"ターン{first_turn}〜{report.turn}"
    parts: list[str] = [f"## ⚔ {turn_range}の結果\n"]
    if auto_note:
        parts.append(auto_note + "\n")
    if started_new_battle and new_save.battle:
        parts.append(f"新しい戦いが始まった: **{new_save.battle.name}**")
        if intro_note:
            parts.append(f"> {intro_note}")
        parts.append("")
    parts.append("```")
    parts.extend(all_lines)
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
    if parsed.free_text and not auto_match:
        parts.append("> ℹ 自由記述からは「フルオート N」だけを解釈します(例: フルオート 5)。\n")
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
    # 誓約checkbox: フォームの表示文言 → balance.constraints のID(先頭一致。未知の文言は無視)
    table = known_constraints(balance)
    constraints = []
    for text in parsed.oath_labels:
        cid = next(
            (k for k, v in table.items() if str(v.get("label", "")) and text.startswith(str(v["label"]))),
            None,
        )
        if cid and cid not in constraints:
            constraints.append(cid)
    spell, used_ai = generation.generate_spell(
        new_save, world, balance, ai, member, parsed.slot, parsed.incantation, is_ult, constraints
    )
    generation.install_spell(new_save, member, parsed.slot, spell, constraints)
    new_save.spell_tokens -= 1
    pending = new_save.pending_update
    if pending and pending.get("member_role") == parsed.member_role and pending.get("slot") == parsed.slot:
        new_save.pending_update = None  # 生成でスロットが変わったら古い進化提案は無効化
    new_save.journal.append(f"{member.name}が新しい技「{spell['name']}」を紡いだ(旧「{old_name}」)")

    source_note = "" if used_ai else "\n> ⚠ AI生成が利用できなかったため、ルール層のテンプレートで代替しました。"
    oath_note = ""
    if constraints:
        labels = "、".join(str(table[c].get("label", c)) for c in constraints)
        oath_note = f"\n⛓ 誓約: {labels}(予算×{constraint_multiplier(constraints, balance):.2f})"
    reply = (
        f"## ✨ 技生成の儀式 — 完了\n\n"
        f"{member.name}の**{parsed.slot}**が「{old_name}」から生まれ変わった:\n\n"
        f"{_spell_block(spell)}\n"
        f"{oath_note}{source_note}\n\n"
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


# ---- [REWIND] ------------------------------------------------------------


def _handle_rewind(save: Save, body: str, balance: dict[str, Any], root: str, repo_slug: str) -> tuple[Save, str]:
    """時戻し: 現在の戦闘の記録上最古のコミットから save/ を復元する(コスト=技生成権1)。

    git履歴は改変しない(復元は新しいコミットとして積まれる)。ワークツリーは触らず、
    対象コミットのファイルを一時ディレクトリに展開して読む(失敗してもセーブ不変)。
    """
    import tempfile

    parsed = parse_rewind_body(body)
    if parsed.errors:
        raise _Invalid(
            "## ⚠ 入力が不正です\n\n" + "\n".join(f"- {e}" for e in parsed.errors) + f"\n\n{_links(repo_slug)}"
        )
    if save.battle is None or not save.battle.active:
        raise _Invalid(
            f"## ⚠ 戻る戦いがありません\n\n時戻しは戦闘中のみ行えます。\n\n{_links(repo_slug)}"
        )
    if save.spell_tokens < 1:
        raise _Invalid(
            "## ⚠ 技生成権がありません\n\n時戻しの代償は技生成権1です。"
            f"レベルアップで獲得してから使ってください。\n\n{_links(repo_slug)}"
        )
    current_name = save.battle.name
    target_sha: str | None = None
    target_turn = 0
    try:
        for sha in gitops.history_for_path(f"{SAVE_DIR}/state.json", cwd=root):
            state = json.loads(gitops.show_file(sha, f"{SAVE_DIR}/state.json", cwd=root))
            b = state.get("battle") or {}
            if not b.get("active") or str(b.get("name")) != current_name:
                break  # この戦いが始まる前のコミットに到達
            target_sha = sha
            target_turn = int(b.get("turn", 1))
    except (gitops.GitError, json.JSONDecodeError, ValueError, KeyError) as e:
        print(f"rewind: history scan failed ({type(e).__name__})")
        raise _Invalid(
            "## ⚠ 時戻しに失敗\n\n戦いの記録をgit履歴から辿れませんでした。"
            f"(生成権は消費されていません)\n\n{_links(repo_slug)}"
        )
    if target_sha is None:
        raise _Invalid(
            "## ⚠ 戻れる時点がありません\n\nこの戦いはまだ履歴に記録されていません。"
            f"最初のターンを解決してから使えます。(生成権は消費されていません)\n\n{_links(repo_slug)}"
        )
    if target_turn >= save.battle.turn:
        raise _Invalid(
            f"## ⚠ これ以上戻れません\n\n既にこの戦いの記録最古の時点(ターン{save.battle.turn})にいます。"
            f"(生成権は消費されていません)\n\n{_links(repo_slug)}"
        )
    try:
        with tempfile.TemporaryDirectory() as td:
            for rel in gitops.list_files(target_sha, SAVE_DIR, cwd=root):
                content = gitops.show_file(target_sha, rel, cwd=root)
                dest = Path(td) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content + "\n", encoding="utf-8")
            restored = load_save(Path(td) / SAVE_DIR)
    except Exception as e:
        print(f"rewind: restore failed ({type(e).__name__})")
        raise _Invalid(
            "## ⚠ 時戻しに失敗\n\n過去のセーブの復元に失敗しました。セーブは変更されていません。"
            f"\n\n{_links(repo_slug)}"
        )
    # 代償は「現在の」所持数から引く(復元値から引くと、同じ地点へ何度戻っても合計1で済んでしまう)。
    # 戦闘中に生成権は増えないので、現在値 ≤ 復元値。時戻しを重ねるほど確実に減る。
    restored.spell_tokens = max(0, save.spell_tokens - 1)
    # 処理済みIssueは現在の記録と統合する(巻き戻しで過去のIssueが未処理に戻らないように)
    restored.processed_issues = list(dict.fromkeys([*restored.processed_issues, *save.processed_issues]))
    del restored.processed_issues[:-PROCESSED_ISSUES_MAX]
    # PR攻撃は「戦場の外」で起きている(実PRとマージ済みの歪みは巻き戻せない)。
    # 状態は現在のものを引き継ぎ、猶予ターンだけ戻した時間に合わせて張り直す。
    # 与えたダメージは無かったことになるので蓄積はリセット(時戻しでの削り稼ぎを防ぐ)。
    if save.battle is not None and save.battle.pr_attack and restored.battle is not None:
        carried = dict(save.battle.pr_attack)
        if "deadline_turn" in carried:
            remaining = int(carried["deadline_turn"]) - save.battle.turn
            carried["deadline_turn"] = restored.battle.turn + max(0, remaining)
        carried["damage_since"] = 0
        restored.battle.pr_attack = carried
        restored.battle.recent_log.append("……だが、既に開かれた禁忌の門は時を遡らない。")
    restored.journal.append(
        f"時戻しの星片を砕いた——「{current_name}」の記録最古の時点(ターン{target_turn})へ(技生成権-1)"
    )
    if restored.battle:
        restored.battle.recent_log.append("⏪ 時が巻き戻った……星の巡りが再び動き出す。")
        del restored.battle.recent_log[: -battle_mod.RECENT_LOG_LIMIT]
    reply = (
        f"## ⏪ 時戻しの儀式 — 完了\n\n"
        f"「{current_name}」は**ターン{target_turn}の開始時点**へ巻き戻った。\n\n"
        f"- 残り技生成権: **{restored.spell_tokens}**\n"
        f"- 乱数も巻き戻っています。同じ手は同じ結末を辿ります——異なる選択を。\n\n"
        f"{_links(repo_slug)}"
    )
    return restored, reply


# ---- PR攻撃のI/O(実PRの作成・監視・強制マージ・後始末) ------------------


def _override_json_text(balance: dict[str, Any], source_name: str) -> str:
    payload = {
        "scope": "battle",
        "source": source_name,
        "overrides": dict(balance.get("pr_attack", {}).get("override", {})),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _base_branch(root_path: Path) -> str:
    try:
        return gitops.current_branch(cwd=str(root_path))
    except gitops.GitError:
        return "main"


def _process_pr_attack(
    save: Save, gh: GhApi | None, repo_slug: str, root_path: Path, balance: dict[str, Any]
) -> list[str]:
    """battle.pr_attack の状態に応じたI/Oを行い、返信に足す注記行を返す。

    gh が無い環境(ローカル/テスト)ではPRをシミュレートして状態だけ進める(ゲームを止めない)。
    """
    battle = save.battle
    notes: list[str] = []
    if battle is None:
        return notes
    pr = battle.pr_attack
    pa = balance.get("pr_attack", {})
    override_file = root_path / OVERRIDE_PATH

    if not battle.active:
        # 戦闘終了の後始末: 歪みを撤去し、開いたままのPRを封じる
        if override_file.exists():
            override_file.unlink()
            notes.append("🌌 戦いの終わりとともに、星の理の歪みは元へ戻った(battle_override.json 撤去)。")
        if pr and pr.get("status") in ("casting", "deadline") and gh and pr.get("pr_number"):
            try:
                gh.post_comment(int(pr["pr_number"]), "戦いは終わった。この詠唱はもう意味を持たない。\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_")
                gh.close_pull(int(pr["pr_number"]))
                if pr.get("branch"):
                    gh.delete_branch(str(pr["branch"]))
            except RuntimeError as e:
                print(f"pr_attack: cleanup failed ({e})")
            pr["status"] = "closed_battle_end"
        return notes

    if not pr:
        return notes
    status = str(pr.get("status", ""))
    enemy_name = next((e.name for e in battle.enemies if e.id == pr.get("enemy_id")), "ボス")

    if status == "pending":
        deadline = battle.turn + int(pa.get("deadline_turns", 3)) - 1
        pr.update({"deadline_turn": deadline, "damage_since": 0})
        if gh is None:
            pr.update({"status": "casting", "pr_number": 0, "branch": ""})
            notes.append("🕳 ボスの禁忌詠唱が始まった(ローカル実行のためPRはシミュレート)。")
            return notes
        try:
            base = _base_branch(root_path)
            branch = f"boss-attack-{pr.get('enemy_id', 'boss')}-t{battle.turn}"
            gh.create_branch(branch, gh.get_branch_sha(base))
            gh.put_file(
                OVERRIDE_PATH,
                _override_json_text(balance, enemy_name),
                f"boss attack: {OVERRIDE_PATH}",
                branch,
            )
            body = (
                f"## 🕳 禁忌詠唱 — {enemy_name}\n\n"
                f"ボスが**星の理を歪める詠唱**を始めた。このPRは `{OVERRIDE_PATH}` "
                "(戦闘スコープのバランス上書き: 防御が意味を失い、癒しが細る)を持ち込もうとしている。\n\n"
                "### 阻止する方法(どちらか)\n"
                f"1. **打ち破る**: {pa.get('deadline_turns', 3)}ターン以内に詠唱中のボスへ合計 "
                f"**{pa.get('break_damage', 90)}ダメージ** を与える\n"
                "2. **封じる**: このPRを**手動でクローズ**する\n\n"
                f"どちらも間に合わなければ、期限ターンの終わりにこのPRは**強制マージ**され、歪みが戦場を覆う。"
                "歪みは戦闘終了時に自動で撤去される。\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_"
            )
            number = gh.create_pull(f"[Boss Attack] 禁忌詠唱 — {enemy_name}", body, branch, base)
            pr.update({"status": "casting", "pr_number": number, "branch": branch})
            notes.append(
                f"🕳 **{enemy_name}の禁忌詠唱!** PR #{number} が開かれた。"
                f"{pa.get('deadline_turns', 3)}ターン以内に合計{pa.get('break_damage', 90)}ダメージで打ち破るか、"
                "PRを手動クローズで封じなければ、星の理が歪む。"
            )
        except RuntimeError as e:
            print(f"pr_attack: create failed ({e}); retrying next turn")
            # pendingのまま次のターンに再試行(ゲームは止めない)
        return notes

    if status == "broken":
        if gh and pr.get("pr_number"):
            try:
                gh.post_comment(int(pr["pr_number"]), "詠唱は打ち破られた。星の理は守られた。\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_")
                gh.close_pull(int(pr["pr_number"]))
                if pr.get("branch"):
                    gh.delete_branch(str(pr["branch"]))
            except RuntimeError as e:
                print(f"pr_attack: close after break failed ({e})")
        pr["status"] = "broken_closed"
        notes.append("💥 禁忌の詠唱を打ち破った! 開かれていたPRは封じられた。")
        return notes

    if status == "deadline":
        merged = False
        sealed = False
        if gh is None or not pr.get("pr_number"):
            merged = True  # シミュレート(ローカル)
        else:
            try:
                state = gh.get_pull(int(pr["pr_number"]))
                if state.get("merged"):
                    merged = True
                elif state.get("state") == "closed":
                    sealed = True
                else:
                    merged = gh.merge_pull(int(pr["pr_number"]), f"boss attack: {enemy_name}")
                    if not merged:
                        # マージAPIが失敗しても効果は発動する(PRは開いたまま放置されたので)
                        gh.post_comment(int(pr["pr_number"]), "詠唱は完成した。歪みは既に戦場を覆っている。\n\n---\n_Generated by [Claude Code](https://claude.ai/code)_")
                        gh.close_pull(int(pr["pr_number"]))
                        merged = True
            except RuntimeError as e:
                print(f"pr_attack: deadline check failed ({e}); retrying next turn")
                return notes
        if sealed:
            pr["status"] = "sealed"
            save.journal.append(f"{enemy_name}の禁忌のPRを封じ、詠唱を阻止した")
            notes.append("🛡 PRは閉じられていた——禁忌の詠唱は封じられた!")
        elif merged:
            override_file.write_text(_override_json_text(balance, enemy_name), encoding="utf-8")
            pr["status"] = "merged"
            save.journal.append(f"{enemy_name}の禁忌詠唱が完成し、星の理が歪んだ")
            notes.append(
                "🕳 **詠唱完成——星の理が歪んだ。** battle_override.json が適用された"
                "(防御が意味を失い、癒しは細る)。ボスを倒して理を取り戻せ!"
            )
        return notes

    return notes


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
    if ai is None:
        ai = AiClient(config_path=root_path / AI_CONFIG_PATH)

    last_error = ""
    for attempt in range(MAX_PUSH_REPLAYS):
        # balanceはリプレイ毎に読み直す(push競合の同期でbattle_override.jsonが現れ得るため)
        balance = _merged_balance(root_path)
        save = load_save(root_path / SAVE_DIR)
        battle_was_active = save.battle is not None and save.battle.active

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
            elif title.startswith(TITLE_REWIND):
                new_save, reply = _handle_rewind(save, body, balance, root, repo_slug)
            else:
                new_save, reply, _report = _handle_turn(save, body, world, balance, ai, repo_slug)
        except _Invalid as e:
            if gh:
                gh.post_comment(number, e.reply_md)
                gh.close_issue(number)
            print(f"invalid input on issue #{number}; nothing consumed")
            return

        pr_notes = _process_pr_attack(new_save, gh, repo_slug, root_path, balance)
        if pr_notes:
            reply = reply.rstrip() + "\n\n" + "\n".join(f"> {n}" for n in pr_notes) + "\n"

        new_save.processed_issues.append(number)
        del new_save.processed_issues[:-PROCESSED_ISSUES_MAX]

        write_save(new_save, root_path / SAVE_DIR)
        svg = board_mod.build_board_svg(new_save, world, balance)
        board_file = root_path / BOARD_PATH
        board_file.parent.mkdir(parents=True, exist_ok=True)
        board_file.write_text(svg, encoding="utf-8")
        # 戦闘開始時のみシーンSVGを生成(素材合成 or プレースホルダ)。
        # 失敗時は古いシーンを消すので、存在チェック=今の戦闘のシーンであることが保証される
        started_battle = (
            not battle_was_active and new_save.battle is not None and new_save.battle.active
        )
        if started_battle:
            prepare_scene(root_path, new_save, world, allow_generation=not ai.mock and attempt == 0)
        has_scene = (root_path / SCENE_PATH).exists()
        cache_key = f"i{number}-a{attempt}"  # Issue番号で一意(camoキャッシュ回避)
        (root_path / README_PATH).write_text(
            screen.render_readme(new_save, world, repo_slug, cache_key, has_scene=has_scene),
            encoding="utf-8",
        )

        pushed = True
        if do_git:
            gitops.configure_identity(root)
            commit_paths = [SAVE_DIR, ASSETS_DIR, README_PATH]
            # PR攻撃のoverrideは生成/削除の両方をコミットに含める(未追跡かつ不在ならaddできないので外す)
            if (root_path / OVERRIDE_PATH).exists() or gitops.is_tracked(OVERRIDE_PATH, cwd=root):
                commit_paths.append(OVERRIDE_PATH)
            gitops.commit(commit_paths, f"apply issue #{number}", cwd=root)
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
