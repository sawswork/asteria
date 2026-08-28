"""M2の受入フローをモックAIで検証する。

- 技を生成→戦闘で使用→アップデートの一連
- 敵AI(知能層)のヒーラー狙いと挑発ロック遵守
- 敵生成・勧誘・フォールバック
"""
from __future__ import annotations

from pathlib import Path

from engine.ai_client import AiClient
from engine.save_io import load_save, write_save
from engine.turn_runner import process_issue
from tests.test_turn_runner import (
    REPO,
    FakeGhApi,
    all_normal,
    body_from,
    make_issue,
    make_root,
)

ROOT = Path(__file__).resolve().parent.parent


def mock_ai() -> AiClient:
    return AiClient(mock=True, fixtures_dir=ROOT / "fixtures/ai")


def broken_ai(tmp_path: Path) -> AiClient:
    empty = tmp_path / "no_fixtures"
    empty.mkdir(exist_ok=True)
    return AiClient(mock=True, fixtures_dir=empty)


def run(root: Path, issue: dict, gh=None, ai=None) -> None:
    process_issue(issue, REPO, str(root), do_git=False, gh=gh, ai=ai or mock_ai())


def gen_body(member: str, slot: str, incantation: str) -> str:
    return (
        f"### 対象メンバー\n\n{member}\n\n### スロット\n\n{slot}\n\n"
        f"### 詠唱文\n\n{incantation}\n"
    )


def update_body(member: str, slot: str, choice: str, direction: str = "") -> str:
    return (
        f"### 対象メンバー\n\n{member}\n\n### スロット\n\n{slot}\n\n"
        f"### 選択\n\n{choice}\n\n### 方向性(任意)\n\n{direction or '_No response_'}\n"
    )


def _grant_token(root: Path, tokens: int = 1) -> None:
    save = load_save(root / "save")
    save.spell_tokens = tokens
    write_save(save, root / "save")


# ---- 技生成 --------------------------------------------------------------


def test_generate_spell_flow(tmp_path):
    root = make_root(tmp_path)
    _grant_token(root)
    gh = FakeGhApi()
    run(root, make_issue(1, gen_body("アタッカー", "アビ2", "星の光を一点に集める穿孔の一撃を"), title="[GENERATE] 技生成の儀式"), gh=gh)
    save = load_save(root / "save")
    attacker = save.member_by_role("attacker")
    assert attacker.abilities[1].name == "星穿ちの牙"  # モックAIの技が装着された
    assert save.spell_tokens == 0
    assert gh.closed == [1]
    assert "技生成の儀式 — 完了" in gh.comments[0][1]
    # 旧技のファイルは魔導書に残る
    assert (root / "save/spells/sora_a2.json").exists()
    assert (root / "save/spells" / f"{attacker.abilities[1].id}.json").exists()


def test_generate_without_token_rejected(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    run(root, make_issue(1, gen_body("アタッカー", "アビ1", "なんかすごいの"), title="[GENERATE] 技生成の儀式"), gh=gh)
    save = load_save(root / "save")
    assert save.member_by_role("attacker").abilities[0].name == "星走り"  # 変化なし
    assert 1 not in save.processed_issues  # 消費なし
    assert "技生成権がありません" in gh.comments[0][1]


def test_generate_during_battle_rejected(tmp_path):
    root = make_root(tmp_path)
    _grant_token(root)
    run(root, make_issue(1, body_from(all_normal())))  # 戦闘開始
    gh = FakeGhApi()
    run(root, make_issue(2, gen_body("アタッカー", "アビ1", "つよいの"), title="[GENERATE] 技生成の儀式"), gh=gh)
    save = load_save(root / "save")
    assert save.spell_tokens == 1  # 消費されていない
    assert "戦闘中は儀式を行えません" in gh.comments[0][1]


def test_generate_falls_back_when_ai_unavailable(tmp_path):
    root = make_root(tmp_path)
    _grant_token(root)
    gh = FakeGhApi()
    run(
        root,
        make_issue(1, gen_body("ヒーラー", "アビ3", "星の雫で癒やす歌"), title="[GENERATE] 技生成の儀式"),
        gh=gh,
        ai=broken_ai(tmp_path),
    )
    save = load_save(root / "save")
    healer = save.member_by_role("healer")
    assert healer.abilities[2].name == "星の雫で癒やす歌"[:12]  # 詠唱文由来のフォールバック名
    assert save.spell_tokens == 0
    assert "ルール層のテンプレート" in gh.comments[0][1]


def test_generated_spell_usable_in_battle(tmp_path):
    root = make_root(tmp_path)
    _grant_token(root)
    run(root, make_issue(1, gen_body("アタッカー", "アビ2", "穿て"), title="[GENERATE] 技生成の儀式"))
    gh = FakeGhApi()
    cmds = all_normal()
    cmds["attacker"] = ("アビ2", "自動")
    run(root, make_issue(2, body_from(cmds)), gh=gh)
    assert any("星穿ちの牙" in c for _, c in gh.comments)  # 生成技が戦闘ログに登場


# ---- 技アップデート ------------------------------------------------------


def test_update_two_step_flow(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    run(root, make_issue(1, update_body("アタッカー", "アビ1", "提案を見る", "もっと重く"), title="[UPDATE] 技アップデート"), gh=gh)
    save = load_save(root / "save")
    assert save.pending_update is not None
    assert save.pending_update["slot"] == "アビ1"
    assert "進化方向3案" in gh.comments[0][1]
    assert "星走り・宙貫" in gh.comments[0][1]

    run(root, make_issue(2, update_body("アタッカー", "アビ1", "案2"), title="[UPDATE] 技アップデート"), gh=gh)
    save = load_save(root / "save")
    attacker = save.member_by_role("attacker")
    assert attacker.abilities[0].name == "星走り・迅"
    assert attacker.abilities[0].ct == 1
    assert save.pending_update is None
    assert "技アップデート — 完了" in gh.comments[1][1]


def test_update_choice_without_pending_rejected(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    run(root, make_issue(1, update_body("アタッカー", "アビ1", "案1"), title="[UPDATE] 技アップデート"), gh=gh)
    save = load_save(root / "save")
    assert save.member_by_role("attacker").abilities[0].name == "星走り"
    assert "選択できる提案がありません" in gh.comments[0][1]


def test_update_preserves_usage_stats(tmp_path):
    root = make_root(tmp_path)
    save = load_save(root / "save")
    save.member_by_role("attacker").abilities[0].usage_count = 7
    save.member_by_role("attacker").abilities[0].kills = 2
    write_save(save, root / "save")
    run(root, make_issue(1, update_body("アタッカー", "アビ1", "提案を見る"), title="[UPDATE] 技アップデート"))
    run(root, make_issue(2, update_body("アタッカー", "アビ1", "案1"), title="[UPDATE] 技アップデート"))
    save = load_save(root / "save")
    ability = save.member_by_role("attacker").abilities[0]
    assert ability.usage_count == 7 and ability.kills == 2


# ---- 敵生成・知能層・勧誘 ------------------------------------------------


def _force_victory(root: Path) -> None:
    save = load_save(root / "save")
    save.battle.enemies[0].hp = 1
    write_save(save, root / "save")


def test_second_battle_uses_generated_enemy(tmp_path):
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))  # 初戦=固定の仔狼
    _force_victory(root)
    run(root, make_issue(2, body_from(all_normal())))  # 勝利
    gh = FakeGhApi()
    run(root, make_issue(3, body_from(all_normal())), gh=gh)  # 新しい戦闘=生成敵
    save = load_save(root / "save")
    assert save.battle.enemies[0].name == "影喰いの豹"  # モックAI由来
    assert save.battle.enemies[0].intelligent is True
    assert "影喰いの豹との戦い" in gh.comments[0][1]


def test_intelligent_enemy_targets_healer(tmp_path):
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))
    _force_victory(root)
    run(root, make_issue(2, body_from(all_normal())))
    run(root, make_issue(3, body_from(all_normal())))  # 影喰いの豹(intelligent)登場ターン
    gh = FakeGhApi()
    run(root, make_issue(4, body_from(all_normal())), gh=gh)
    comment = gh.comments[0][1]
    # 知能層: ヘイト無視でヒーラー(ミオ)を狙い、セリフ付き
    assert "まず癒し手から狩る" in comment
    assert "ミオに" in comment


def test_taunt_lock_beats_intelligent_ai(tmp_path):
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))
    _force_victory(root)
    run(root, make_issue(2, body_from(all_normal())))
    run(root, make_issue(3, body_from(all_normal())))
    # タンクが挑発 → AIがヒーラーを狙ってもロックが強制する
    cmds = all_normal()
    cmds["tank"] = ("アビ1", "自動")
    run(root, make_issue(4, body_from(cmds)))
    gh = FakeGhApi()
    run(root, make_issue(5, body_from(all_normal())), gh=gh)
    comment = gh.comments[0][1]
    assert "挑発から逃れられない" in comment
    assert "ガンテに" in comment
    assert "ミオに" not in comment


def test_recruit_after_configured_victories(tmp_path):
    root = make_root(tmp_path)
    save = load_save(root / "save")
    save.stats["victories"] = 2  # 次の勝利が3勝目=勧誘イベント
    write_save(save, root / "save")
    run(root, make_issue(1, body_from(all_normal())))  # 戦闘開始
    _force_victory(root)
    gh = FakeGhApi()
    run(root, make_issue(2, body_from(all_normal())), gh=gh)  # 3勝目
    save = load_save(root / "save")
    assert len(save.roster_extra) == 1
    assert save.roster_extra[0].name == "ノクト"
    assert "勧誘イベント" in gh.comments[0][1]
    # 控えメンバーもファイルとして保存される
    assert (root / "save/party/recruit1.json").exists()


def test_enemy_generation_falls_back_without_ai(tmp_path):
    root = make_root(tmp_path)
    ai = broken_ai(tmp_path)
    run(root, make_issue(1, body_from(all_normal())), ai=ai)
    _force_victory(root)
    run(root, make_issue(2, body_from(all_normal())), ai=ai)
    run(root, make_issue(3, body_from(all_normal())), ai=ai)
    save = load_save(root / "save")
    assert save.battle is not None and save.battle.active
    fallback_names = {"星屑の亡霊", "蝕まれた岩甲獣", "夜哭きの梟"}
    assert save.battle.enemies[0].name in fallback_names