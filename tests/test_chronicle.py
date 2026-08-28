"""年代記(save/chronicle/): 書籍化の素材となる全文記録のテスト。"""
from __future__ import annotations

from pathlib import Path

from engine import chronicle
from engine.ai_client import AiClient
from engine.save_io import load_save, write_save
from engine.turn_runner import process_issue
from tests.test_turn_runner import REPO, FakeGhApi, all_normal, body_from, make_issue, make_root

ROOT = Path(__file__).resolve().parent.parent


def _mock_ai() -> AiClient:
    return AiClient(mock=True, fixtures_dir=ROOT / "fixtures/ai")


def _chapters(root: Path) -> list[Path]:
    return sorted((root / "save" / chronicle.CHRONICLE_DIR).glob("*.md"))


# ---- 純粋関数 ------------------------------------------------------------


def test_append_entry_is_idempotent_and_keeps_order():
    text = chronicle.append_entry("", 1, "ターン1", "a")
    text = chronicle.append_entry(text, 2, "ターン2", "b")
    text = chronicle.append_entry(text, 1, "ターン1", "a-again")  # リプレイでの再処理
    text = chronicle.append_entry(text, 3, "ターン3", "c")
    assert text.count("<!-- issue:1 -->") == 1  # 二重に積まれない
    assert text.index("a-again") < text.index("b") < text.index("c")  # 時系列が崩れない


def test_chapter_number_is_derived_from_record():
    """章番号は戦績から導出する(この機能より前から続くセーブでも正しい章に落ちる)。"""
    assert chronicle.chapter_number({}, battle_active=False) == 1  # まだ何も起きていない
    assert chronicle.chapter_number({}, battle_active=True) == 1  # 第1戦の最中
    mid = {"victories": 7, "defeats": 1}
    assert chronicle.chapter_number(mid, battle_active=True) == 9  # 第9戦の最中
    assert chronicle.chapter_number(mid, battle_active=False) == 8  # 第8戦の直後(拠点)


def test_chapter_filename_is_sortable():
    assert chronicle.chapter_filename(1) == "chapter-001.md"
    assert chronicle.chapter_filename(12) == "chapter-012.md"
    names = [chronicle.chapter_filename(n) for n in (2, 10, 1)]
    assert sorted(names) == [chronicle.chapter_filename(n) for n in (1, 2, 10)]


# ---- ランナー統合 --------------------------------------------------------


def test_turns_are_recorded_in_full(tmp_path):
    """直近10行しか残らないrecent_logと違い、年代記は全ターンの本文を残す。"""
    root = make_root(tmp_path)
    gh = FakeGhApi()
    process_issue(
        make_issue(1, body_from(all_normal(), free_text="フルオート 3")),
        REPO, str(root), do_git=False, gh=gh, ai=_mock_ai(),
    )
    chapters = _chapters(root)
    assert len(chapters) == 1
    text = chapters[0].read_text(encoding="utf-8")
    assert "# 第1章" in text and "**敵**" in text and "**一党**" in text
    for turn in (1, 2, 3):
        assert f"—— ターン{turn} ——" in text  # 3ターン分すべて残る
    save = load_save(root / "save")
    assert len(save.battle.recent_log) <= 10  # ボード用の直近ログは従来どおり短い


def test_replayed_issue_does_not_duplicate(tmp_path):
    """同じIssueを再処理しても本文が二重にならない(push競合のリプレイ対策)。"""
    import shutil

    root = make_root(tmp_path)
    gh = FakeGhApi()
    issue = make_issue(1, body_from(all_normal()))
    # 実際のリプレイは sync_with_remote でセーブごとターン前へ戻ってから再解決する
    before = tmp_path / "save-before"
    shutil.copytree(root / "save", before)
    process_issue(issue, REPO, str(root), do_git=False, gh=gh, ai=_mock_ai())
    text_once = _chapters(root)[0].read_text(encoding="utf-8")

    shutil.rmtree(root / "save")
    shutil.copytree(before, root / "save")  # リモート先端へ巻き戻した状態を再現
    process_issue(issue, REPO, str(root), do_git=False, gh=gh, ai=_mock_ai())
    text_twice = _chapters(root)[0].read_text(encoding="utf-8")
    assert text_twice.count("<!-- issue:1 -->") == 1  # 二重に積まれない
    assert text_once == text_twice  # 同じターンの再解決なので内容も一致する


def test_new_battle_starts_a_new_chapter(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    ai = _mock_ai()
    process_issue(
        make_issue(1, body_from(all_normal(), free_text="フルオート 8")),
        REPO, str(root), do_git=False, gh=gh, ai=ai,
    )
    save = load_save(root / "save")
    save.battle.enemies[0].hp = 1  # 次の一撃で決着させる
    write_save(save, root / "save")
    process_issue(make_issue(2, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh, ai=ai)
    process_issue(make_issue(3, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh, ai=ai)
    chapters = _chapters(root)
    assert len(chapters) == 2  # 戦闘ごとに章が変わる
    assert "**幕引き**" in chapters[0].read_text(encoding="utf-8")  # 前章は勝敗で締められる


def test_incantation_and_oath_are_preserved(tmp_path):
    """プレイヤー自身の詠唱文と誓約は書籍の核なので必ず残す。"""
    root = make_root(tmp_path)
    save = load_save(root / "save")
    save.spell_tokens = 1
    write_save(save, root / "save")
    gh = FakeGhApi()
    body = (
        "### 対象メンバー\n\nアタッカー\n\n### スロット\n\nアビ2\n\n"
        "### 誓約\n\n- [x] 1戦闘に1回だけ(予算×1.4)\n\n"
        "### 詠唱文\n\n星の光を一点に集める穿孔の一撃を\n"
    )
    process_issue(
        make_issue(1, body, title="[GENERATE] 技生成の儀式"),
        REPO, str(root), do_git=False, gh=gh, ai=_mock_ai(),
    )
    text = _chapters(root)[0].read_text(encoding="utf-8")
    assert "技生成の儀式" in text
    assert "星の光を一点に集める穿孔の一撃を" in text  # 詠唱文がそのまま残る
    assert "⛓ 誓約" in text and "1戦闘に1回だけ" in text


def test_chronicle_failure_does_not_stop_the_game(tmp_path, monkeypatch):
    """記録に失敗しても冒険は止まらない(記録は大切だが進行を人質に取らない)。"""
    import engine.turn_runner as tr

    root = make_root(tmp_path)
    gh = FakeGhApi()
    monkeypatch.setattr(
        tr.chronicle, "append_entry",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    process_issue(make_issue(1, body_from(all_normal())), REPO, str(root), do_git=False, gh=gh, ai=_mock_ai())
    assert load_save(root / "save").battle is not None  # ターンは通っている
    assert gh.closed == [1]
