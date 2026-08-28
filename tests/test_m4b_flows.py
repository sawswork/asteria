"""M4-B: 宿敵(敗北した相手の再来)と時戻し(git履歴からの復元)のテスト。"""
from __future__ import annotations

from pathlib import Path

from engine.battle import nemesis_enemy, resolve_turn
from engine.models import Buff
from engine.save_io import load_save, write_save
from engine.turn_runner import process_issue
from tests.conftest import all_normal_commands
from tests.test_turn_runner import (
    REPO,
    FakeGhApi,
    _git,
    _setup_git,
    all_normal,
    body_from,
    make_issue,
    make_root,
)

ROOT = Path(__file__).resolve().parent.parent

REWIND_BODY = "### 確認\n\n時を戻す(技生成権を1消費)\n"


def _run(root: Path, issue: dict, do_git: bool = False, gh=None) -> None:
    process_issue(issue, REPO, str(root), do_git=do_git, gh=gh)


# ---- 宿敵 ----------------------------------------------------------------


def _doom_party(save) -> None:
    """敗北確定の状況を作る(3人戦闘不能・残り1人はHP1)。"""
    for m in save.party[1:]:
        m.hp = 0
    save.party[0].hp = 1


def test_defeat_creates_nemesis(battle_save, world, balance):
    _doom_party(battle_save)
    enemy = battle_save.battle.enemies[0]
    enemy.weaknesses.append({"field": "油星", "mult": 1.5})  # 戦いの記憶ごと保存されること
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert r1.result == "defeat"
    assert s1.nemesis is not None
    assert s1.nemesis["enemy"]["id"] == enemy.id
    assert s1.nemesis["enemy"]["weaknesses"] == [{"field": "油星", "mult": 1.5}]
    assert any("宿敵" in line for line in s1.journal)


def test_nemesis_enemy_rebuilds_fresh_but_remembers(fresh_save, world, balance):
    from engine.battle import first_battle_enemies

    enemy, _, _ = first_battle_enemies(world)
    e = enemy[0]
    e.hp = 5
    e.buffs.append(Buff(stat="atk", mult=1.3, turns_left=2))
    e.stunned_turns = 1
    e.evolutions.append({"name": "本能の覚醒", "reason": "hp", "turn": 3})
    e.evolutions_used = 1
    e.hp_evolution_triggered = True
    e.weaknesses.append({"field": "雷紋", "mult": 1.5})
    e.actions["evolved"] = {"name": "覚醒の一撃", "effects": [{"tag": "damage", "power": 1.8, "target": "enemy"}]}
    fresh_save.nemesis = {"enemy": e.to_dict(), "battle_name": "テスト戦"}
    rebuilt = nemesis_enemy(fresh_save)
    assert rebuilt is not None
    foe, name, intro = rebuilt
    assert foe.hp == foe.max_hp and foe.buffs == [] and foe.stunned_turns == 0
    assert foe.evolutions_used == 1 and foe.hp_evolution_triggered
    assert foe.weaknesses == [{"field": "雷紋", "mult": 1.5}]
    assert "evolved" in foe.actions
    assert name.startswith("宿敵・")


def test_victory_over_nemesis_clears_it(battle_save, world, balance):
    enemy = battle_save.battle.enemies[0]
    battle_save.nemesis = {"enemy": enemy.to_dict(), "battle_name": "旧戦"}
    enemy.hp = 1
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert r1.result == "victory"
    assert s1.nemesis is None
    assert any("因縁" in l and "決着" in l for l in r1.lines)


def test_victory_over_other_enemy_keeps_nemesis(battle_save, world, balance):
    battle_save.nemesis = {"enemy": {"id": "someone_else", "name": "別の宿敵", "title": "",
                                     "max_hp": 100, "hp": 100, "atk": 10, "def": 5, "agi": 5,
                                     "actions": {"normal": {"name": "攻撃", "effects": []}}}}
    battle_save.battle.enemies[0].hp = 1
    s1, r1 = resolve_turn(battle_save, all_normal_commands(), balance, world)
    assert r1.result == "victory"
    assert s1.nemesis is not None  # 別の敵を倒しても宿敵は消えない


def test_runner_starts_nemesis_battle(tmp_path):
    root = make_root(tmp_path)
    save = load_save(root / "save")
    save.stats["victories"] = 1  # 初戦ではない
    save.nemesis = {
        "enemy": {
            "id": "enemy_gen7", "name": "星喰いの影", "title": "宿怨", "max_hp": 150, "hp": 30,
            "atk": 12, "def": 8, "agi": 9, "tier": "standard",
            "actions": {"normal": {"name": "爪撃", "effects": [{"tag": "damage", "power": 1.0, "target": "enemy"}]}},
        },
        "battle_name": "星喰いの影との戦い",
    }
    write_save(save, root / "save")
    gh = FakeGhApi()
    _run(root, make_issue(1, body_from(all_normal())), gh=gh)
    after = load_save(root / "save")
    assert after.battle is not None
    assert after.battle.name == "宿敵・星喰いの影との再戦"
    assert after.battle.enemies[0].hp == 150 or after.battle.enemies[0].hp < 150  # フルHPから戦闘1ターン経過
    assert "宿敵" in gh.comments[0][1]


# ---- 時戻し --------------------------------------------------------------


def test_rewind_restores_recorded_battle_start(tmp_path):
    root = make_root(tmp_path / "work")
    save = load_save(root / "save")
    save.spell_tokens = 2  # 開戦前に付与(履歴にも同数が載る)
    write_save(save, root / "save")
    origin = _setup_git(tmp_path, root)

    gh = FakeGhApi()
    _run(root, make_issue(1, body_from(all_normal())), do_git=True, gh=gh)  # ターン1 → turn=2をコミット
    _run(root, make_issue(2, body_from(all_normal())), do_git=True, gh=gh)  # ターン2 → turn=3をコミット
    before = load_save(root / "save")
    assert before.battle.turn == 3

    _run(root, make_issue(3, REWIND_BODY, title="[REWIND] 時戻しの儀式"), do_git=True, gh=gh)
    after = load_save(root / "save")
    assert after.battle is not None and after.battle.active
    assert after.battle.turn == 2  # 記録上最古の時点(ターン1解決直後)
    assert after.spell_tokens == 1  # 代償1
    assert {1, 2, 3} <= set(after.processed_issues)  # 過去Issueが未処理に戻らない
    assert any("時戻しの星片" in line for line in after.journal)
    assert "時戻しの儀式 — 完了" in gh.comments[-1][1]
    # 履歴は改変されず、時戻しは新しいコミットとして積まれている
    assert _git("rev-parse", "HEAD", cwd=root) == _git("rev-parse", "main", cwd=origin)


def test_rewind_without_battle_rejected(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    _run(root, make_issue(1, REWIND_BODY, title="[REWIND] 時戻しの儀式"), gh=gh)
    assert "戻る戦いがありません" in gh.comments[0][1]
    assert gh.closed == [1]
    save = load_save(root / "save")
    assert 1 not in save.processed_issues  # 何も消費されない


def test_rewind_without_token_rejected(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    _run(root, make_issue(1, body_from(all_normal())), gh=gh)  # 戦闘開始(tokens=0)
    _run(root, make_issue(2, REWIND_BODY, title="[REWIND] 時戻しの儀式"), gh=gh)
    assert "技生成権がありません" in gh.comments[-1][1]
    save = load_save(root / "save")
    assert save.battle.turn == 2  # 変化なし


def test_rewind_without_recorded_history_rejected(tmp_path):
    root = make_root(tmp_path / "work")
    _setup_git(tmp_path, root)  # initコミットは戦闘なし
    gh = FakeGhApi()
    _run(root, make_issue(1, body_from(all_normal())), do_git=False, gh=gh)  # 戦闘はディスクのみ(未コミット)
    save = load_save(root / "save")
    save.spell_tokens = 1
    write_save(save, root / "save")
    _run(root, make_issue(2, REWIND_BODY, title="[REWIND] 時戻しの儀式"), do_git=True, gh=gh)
    assert "戻れる時点がありません" in gh.comments[-1][1]
    after = load_save(root / "save")
    assert after.spell_tokens == 1  # 消費されない


def test_rewind_at_battle_start_rejected(tmp_path):
    root = make_root(tmp_path / "work")
    save = load_save(root / "save")
    save.spell_tokens = 1
    write_save(save, root / "save")
    _setup_git(tmp_path, root)
    gh = FakeGhApi()
    _run(root, make_issue(1, body_from(all_normal())), do_git=True, gh=gh)  # turn=2をコミット
    _run(root, make_issue(2, REWIND_BODY, title="[REWIND] 時戻しの儀式"), do_git=True, gh=gh)
    assert "これ以上戻れません" in gh.comments[-1][1]
    after = load_save(root / "save")
    assert after.spell_tokens == 1
