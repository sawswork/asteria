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
        self.existing_pull = 0

    def get_branch_sha(self, branch: str) -> str:
        return "abc123"

    def find_open_pull_by_head(self, branch: str) -> int:
        return self.existing_pull

    def branch_exists(self, name: str) -> bool:
        return name in [b for b, _ in self.branches]

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


def test_rewind_carries_pr_attack_forward(tmp_path):
    """時戻ししてもPR攻撃は巻き戻らない(猶予は据え置き・削りはリセット)。"""
    from engine.save_io import load_save, write_save
    from engine.turn_runner import process_issue
    from tests.test_turn_runner import _setup_git, all_normal, body_from, make_issue

    root = make_root(tmp_path / "work")
    save = load_save(root / "save")
    save.spell_tokens = 1
    write_save(save, root / "save")
    _setup_git(tmp_path, root)
    gh = FakePrGhApi()  # 時戻し中もPR状態の確認が走るため、PR APIを持つ代替を使う
    process_issue(make_issue(1, body_from(all_normal())), REPO, str(root), do_git=True, gh=gh)
    process_issue(make_issue(2, body_from(all_normal())), REPO, str(root), do_git=True, gh=gh)

    mid = load_save(root / "save")
    assert mid.battle.turn == 3
    mid.battle.pr_attack = {
        "status": "casting", "enemy_id": mid.battle.enemies[0].id,
        "deadline_turn": 5, "damage_since": 70, "pr_number": 77, "branch": "b",
    }
    write_save(mid, root / "save")
    process_issue(
        make_issue(3, "### 確認\n\n時を戻す(技生成権を1消費)\n", title="[REWIND] 時戻しの儀式"),
        REPO, str(root), do_git=True, gh=gh,
    )
    after = load_save(root / "save")
    pr = after.battle.pr_attack
    assert pr is not None and pr["status"] == "casting" and pr["pr_number"] == 77
    assert pr["damage_since"] == 0  # 与ダメージは無かったことになる
    assert pr["deadline_turn"] - after.battle.turn == 2  # 猶予(5-3=2ターン)は据え置き


def test_replay_reuses_existing_pr(tmp_path, battle_save, balance):
    """リプレイで再入しても2本目のPRを開かず、既存PRを引き継ぐ。"""
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {"status": "pending", "enemy_id": enemy.id}
    gh = FakePrGhApi()
    gh.existing_pull = 55
    notes = _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    assert gh.pulls == []  # 新しいPRは作られない
    assert battle_save.battle.pr_attack["pr_number"] == 55
    assert any("既に開かれている" in n for n in notes)


def test_merged_pr_wins_over_replayed_break(tmp_path, battle_save, balance):
    """リプレイで盤面が『ブレイク成立』と再計算しても、実PRがマージ済みならそちらが真実。"""
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {
        "status": "broken", "enemy_id": enemy.id, "pr_number": 77, "branch": "b",
    }
    gh = FakePrGhApi()
    gh.pull_state = {"state": "closed", "merged": True}
    notes = _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    assert battle_save.battle.pr_attack["status"] == "merged"
    assert (tmp_path / OVERRIDE_PATH).exists()
    assert gh.closed_pulls == []  # マージ済みPRを閉じにいかない
    assert any("既に完成していた" in n for n in notes)


def test_cleanup_failure_keeps_state_for_retry(tmp_path, battle_save, balance):
    """戦闘終了の後始末がAPI失敗したら終端状態にせず、次回再試行できるようにする。"""
    enemy = _make_boss(battle_save)
    battle_save.battle.active = False
    battle_save.battle.result = "victory"
    battle_save.battle.pr_attack = {
        "status": "casting", "enemy_id": enemy.id, "pr_number": 77, "branch": "b",
    }
    gh = FakePrGhApi()
    gh.raise_on_comment = True  # post_comment が失敗する
    _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    assert battle_save.battle.pr_attack["status"] == "casting"  # 終端にしない


def test_full_auto_stops_when_boss_starts_casting(tmp_path):
    """ボスの詠唱が始まったらフルオートは止まる(自動送りでPR攻撃を素通りさせない)。"""
    from engine.save_io import load_save, write_save
    from engine.turn_runner import process_issue
    from tests.test_turn_runner import all_normal, body_from, make_issue

    root = make_root(tmp_path)
    save = load_save(root / "save")
    save.stats["victories"] = 1
    write_save(save, root / "save")
    gh = FakePrGhApi()
    # 1ターン目で戦闘を開始し、ボスに差し替えてから自動送りさせる
    process_issue(make_issue(1, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh)
    save = load_save(root / "save")
    e = save.battle.enemies[0]
    e.tier, e.max_hp, e.hp = "boss", 400, 200  # 次のターン終了時にHP60%割れで詠唱開始
    write_save(save, root / "save")
    process_issue(
        make_issue(2, body_from(all_normal(), free_text="フルオート 8")),
        REPO, str(root), do_git=False, gh=gh,
    )
    after = load_save(root / "save")
    assert after.battle.pr_attack is not None
    reply = gh.comments[-1][1]
    assert "禁忌の詠唱を始めた" in reply
    assert "8ターンを自動解決" not in reply  # 8ターン走り切っていない


def test_boss_pr_attack_end_to_end(tmp_path):
    """詠唱開始→期限→強制マージ→overrideが次のターンに効く、までを通しで確認する。"""
    from engine.save_io import load_save, write_save
    from engine.turn_runner import process_issue
    from tests.test_turn_runner import all_normal, body_from, make_issue

    root = make_root(tmp_path)
    save = load_save(root / "save")
    save.stats["victories"] = 1
    write_save(save, root / "save")
    gh = FakePrGhApi()
    process_issue(make_issue(1, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh)

    # ボスに差し替え、HP60%割れの状態にする
    save = load_save(root / "save")
    e = save.battle.enemies[0]
    e.tier, e.max_hp, e.hp, e.atk = "boss", 4000, 2000, 1
    write_save(save, root / "save")

    # 詠唱開始(pending)→ 同じIssue処理内で実PRが開かれ casting になる
    process_issue(make_issue(2, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh)
    save = load_save(root / "save")
    assert save.battle.pr_attack["status"] == "casting"
    assert save.battle.pr_attack["pr_number"] == 77
    assert gh.pulls[0]["title"].startswith("[Boss Attack]")

    # 期限まで削らずに待つ(ダメージが閾値に届かないよう攻撃力を潰しておく)
    for m in save.party:
        m.atk = 1
    deadline = save.battle.pr_attack["deadline_turn"]
    write_save(save, root / "save")
    issue_no = 3
    while load_save(root / "save").battle.turn <= deadline:
        process_issue(make_issue(issue_no, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh)
        issue_no += 1

    save = load_save(root / "save")
    assert save.battle.pr_attack["status"] == "merged"
    assert gh.merged == [77]
    assert (root / OVERRIDE_PATH).exists()  # 歪みが戦場に持ち込まれた
    assert _merged_balance(root)["damage"]["def_coeff"] == 0.1  # 次のターンから適用される

    # 戦闘を終わらせると歪みは撤去され、PRの後始末も済む
    save.battle.enemies[0].hp = 1
    for m in save.party:
        m.atk = 500
    write_save(save, root / "save")
    process_issue(make_issue(issue_no, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh)
    assert not (root / OVERRIDE_PATH).exists()
    assert load_save(root / "save").battle.result == "victory"


def test_full_auto_continues_after_casting_started(tmp_path):
    """詠唱が既に始まっている戦闘では、フルオートは1ターンで止まらず走り切る。"""
    from engine.save_io import load_save, write_save
    from engine.turn_runner import process_issue
    from tests.test_turn_runner import all_normal, body_from, make_issue

    root = make_root(tmp_path)
    save = load_save(root / "save")
    save.stats["victories"] = 1
    write_save(save, root / "save")
    gh = FakePrGhApi()
    process_issue(make_issue(1, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh)
    save = load_save(root / "save")
    e = save.battle.enemies[0]
    e.tier, e.max_hp, e.hp, e.atk = "boss", 8000, 3000, 1
    save.battle.pr_attack = {  # 既に詠唱中
        "status": "casting", "enemy_id": e.id, "deadline_turn": save.battle.turn + 20,
        "damage_since": 0, "pr_number": 77, "branch": "b",
    }
    for m in save.party:
        m.atk = 1
    write_save(save, root / "save")
    before = load_save(root / "save").battle.turn
    process_issue(
        make_issue(2, body_from(all_normal(), free_text="フルオート 5")),
        REPO, str(root), do_git=False, gh=gh,
    )
    after = load_save(root / "save").battle.turn
    assert after - before == 5  # 5ターン走り切る(1ターンで止まらない)


def test_broken_close_failure_keeps_state_for_retry(tmp_path, battle_save, balance):
    """ブレイク後のクローズがAPI失敗したら終端にせず再試行できるようにする。"""
    enemy = _make_boss(battle_save)
    battle_save.battle.pr_attack = {
        "status": "broken", "enemy_id": enemy.id, "pr_number": 77, "branch": "b",
    }
    gh = FakePrGhApi()
    gh.raise_on_comment = True
    _process_pr_attack(battle_save, gh, REPO, tmp_path, balance)
    assert battle_save.battle.pr_attack["status"] == "broken"
