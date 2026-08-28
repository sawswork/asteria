"""M4-C: PR攻撃(禁忌詠唱)のテスト。状態機械はbattle.py、I/Oは_process_pr_attackをモックGhで検証。"""
from __future__ import annotations

import json
from pathlib import Path

from engine.battle import resolve_turn
from engine.save_io import load_json, write_save
from engine.turn_runner import OVERRIDE_PATH, _merged_balance, _process_pr_attack
from tests.conftest import all_normal_commands
from tests.test_turn_runner import FakeGhApi, make_root

ROOT = Path(__file__).resolve().parent.parent

REPO = "owner/repo"


class FakePrGhApi(FakeGhApi):
    """PR攻撃用のGhApi代替。作成・状態・マージ・クローズを記録する。"""

    def __init__(self) -> None:
        super().__init__()
        self.branches: list[tuple[str, str]] = []
        self.files: list[tuple[str, str, str]] = []  # (path, text, branch)
        self.pulls: list[dict] = []
        self.merged: list[int] = []
        self.closed_pulls: list[int] = []
        self.deleted_branches: list[str] = []
        self.pull_state: dict = {"state": "open", "merged": False}
        self.merge_ok = True

    def get_branch_sha(self, branch: str) -> str:
        return "abc123"

    def create_branch(self, name: str, sha: str) -> None:
        self.branches.append((name, sha))

    def put_file(self, path: str, text: str, message: str, branch: str) -> None:
        self.files.append((path, text, branch))

    def create_pull(self, title: str, body: str, head: str, base: str) -> int:
        self.pulls.append({"title": title, "body": body, "head": head, "base": base})
        return 77

    def get_pull(self, number: int) -> dict:
        return dict(self.pull_state)

    def merge_pull(self, number: int, title: str) -> bool:
        if self.merge_ok:
            self.merged.append(number)
        return self.merge_ok

    def close_pull(self, number: int) -> None:
        self.closed_pulls.append(number)

    def delete_branch(self, name: str) -> None:
        self.deleted_branches.append(name)


def _make_boss(battle_save):
    enemy = battle_save.battle.enemies[0]
    enemy.tier = "boss"
    enemy.max_hp = enemy.hp = 1000
    return enemy


# ---- 状態機械(battle.py) ------------------------------------------------


def test_boss_hp_trigger_starts_casting_telegraph(battle_save, world, balance):
    enemy = _make_boss(battle_save)
    enemy.hp = int(enemy.max_hp * 0.5)  # 60%割れ
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    pr = s1.battle.pr_attack
    assert pr is not None and pr["status"] == "pending" and pr["enemy_id"] == enemy.id
    assert any("禁忌の詠唱" in l for l in r1.lines)
    # 2度目のトリガーは無い(1戦闘1回)
    s1.battle.pr_attack["status"] = "broken_closed"
    s2, r2 = resolve_turn(s1, all_normal_commands(), balance, world)
    assert s2.battle.pr_attack["status"] == "broken_closed"


def test_non_boss_never_triggers(battle_save, world, balance):
    enemy = battle_save.battle.enemies[0]  # standard
    enemy.max_hp = enemy.hp = 1000
    enemy.hp = 100
    s1, _ = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert s1.battle.pr_attack is None


def test_casting_breaks_on_enough_damage(battle_save, world, balance):
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {
        "status": "casting", "enemy_id": enemy.id, "deadline_turn": battle_save.battle.turn + 2,
        "damage_since": 0, "pr_number": 77,
    }
    battle_save.member_by_role("attacker").atk = 300  # 一撃で閾値90超え
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert s1.battle.pr_attack["status"] == "broken"
    assert any("打ち破った" in l for l in r1.lines)


def test_casting_reaches_deadline(battle_save, world, balance):
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {
        "status": "casting", "enemy_id": enemy.id, "deadline_turn": battle_save.battle.turn,
        "damage_since": 0, "pr_number": 77,
    }
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert s1.battle.pr_attack["status"] == "deadline"
    assert any("星の理が歪む" in l for l in r1.lines)


def test_damage_to_other_enemy_does_not_count(battle_save, world, balance):
    import copy

    boss = _make_boss(battle_save)
    other = copy.deepcopy(boss)
    other.id = "other"
    other.tier = "standard"
    battle_save.battle.enemies.append(other)
    battle_save.battle.pr_attack = {
        "status": "casting", "enemy_id": boss.id, "deadline_turn": battle_save.battle.turn + 2,
        "damage_since": 0, "pr_number": 77,
    }
    cmds = all_normal_commands()
    for c in cmds.values():
        c.target = "敵2"  # ボス以外を殴る
    s1, _ = resolve_turn(battle_save, cmds, balance, world)
    assert s1.battle.pr_attack["damage_since"] == 0


# ---- I/O(_process_pr_attack) --------------------------------------------


def test_pending_creates_real_pr(tmp_path, battle_save, balance):
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {"status": "pending", "enemy_id": enemy.id}
    gh = FakePrGhApi()
    notes = _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    pr = battle_save.battle.pr_attack
    assert pr["status"] == "casting" and pr["pr_number"] == 77
    assert pr["deadline_turn"] == battle_save.battle.turn + int(balance["pr_attack"]["deadline_turns"]) - 1
    assert gh.pulls and gh.pulls[0]["title"].startswith("[Boss Attack]")
    assert gh.files and gh.files[0][0] == OVERRIDE_PATH
    assert json.loads(gh.files[0][1])["overrides"] == balance["pr_attack"]["override"]
    assert any("PR #77" in n for n in notes)


def test_pending_without_gh_simulates(tmp_path, battle_save, balance):
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {"status": "pending", "enemy_id": enemy.id}
    notes = _process_pr_attack(battle_save, None, REPO, tmp_path, balance)
    assert battle_save.battle.pr_attack["status"] == "casting"
    assert notes


def test_deadline_open_pr_force_merges_and_applies(tmp_path, battle_save, balance):
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {
        "status": "deadline", "enemy_id": enemy.id, "pr_number": 77, "branch": "boss-attack-x",
    }
    gh = FakePrGhApi()
    notes = _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    assert gh.merged == [77]
    assert battle_save.battle.pr_attack["status"] == "merged"
    assert (tmp_path / OVERRIDE_PATH).exists()
    assert any("星の理が歪んだ" in n for n in notes)
    assert any("歪んだ" in line for line in battle_save.journal)


def test_deadline_closed_pr_is_sealed(tmp_path, battle_save, balance):
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {
        "status": "deadline", "enemy_id": enemy.id, "pr_number": 77, "branch": "boss-attack-x",
    }
    gh = FakePrGhApi()
    gh.pull_state = {"state": "closed", "merged": False}
    notes = _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    assert gh.merged == []
    assert battle_save.battle.pr_attack["status"] == "sealed"
    assert not (tmp_path / OVERRIDE_PATH).exists()
    assert any("封じられた" in n for n in notes)


def test_broken_closes_pr(tmp_path, battle_save, balance):
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {
        "status": "broken", "enemy_id": enemy.id, "pr_number": 77, "branch": "boss-attack-x",
    }
    gh = FakePrGhApi()
    _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    assert gh.closed_pulls == [77]
    assert gh.deleted_branches == ["boss-attack-x"]
    assert battle_save.battle.pr_attack["status"] == "broken_closed"


def test_battle_end_cleans_override_and_open_pr(tmp_path, battle_save, balance):
    enemy = _make_boss(battle_save)
    (tmp_path / OVERRIDE_PATH).write_text("{\"overrides\": {}}\n", encoding="utf-8")
    battle_save.battle.active = False
    battle_save.battle.result = "victory"
    battle_save.battle.pr_attack = {
        "status": "casting", "enemy_id": enemy.id, "pr_number": 77, "branch": "boss-attack-x",
    }
    gh = FakePrGhApi()
    notes = _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    assert not (tmp_path / OVERRIDE_PATH).exists()
    assert gh.closed_pulls == [77]
    assert battle_save.battle.pr_attack["status"] == "closed_battle_end"
    assert any("元へ戻った" in n for n in notes)


# ---- override の適用 ------------------------------------------------------


def test_merged_balance_applies_override(tmp_path):
    root = make_root(tmp_path)
    base = load_json(root / "world/balance.json")
    assert _merged_balance(root)["damage"]["def_coeff"] == base["damage"]["def_coeff"]
    (root / OVERRIDE_PATH).write_text(
        json.dumps({"overrides": {"damage": {"def_coeff": 0.1}}}), encoding="utf-8"
    )
    merged = _merged_balance(root)
    assert merged["damage"]["def_coeff"] == 0.1
    assert merged["heal"] == base["heal"]  # 他は不変


def test_next_battle_tier_boss_cadence(fresh_save, balance):
    from engine.generation import next_battle_tier

    fresh_save.stats["victories"] = 7  # 8戦目=boss
    assert next_battle_tier(fresh_save, balance) == "boss"
    fresh_save.stats["victories"] = 3  # 4戦目=elite
    assert next_battle_tier(fresh_save, balance) == "elite"
    fresh_save.stats["victories"] = 4
    assert next_battle_tier(fresh_save, balance) == "standard"
