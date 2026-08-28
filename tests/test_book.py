"""旅の書(book/): 年代記を1冊へ編む処理のテスト。"""
from __future__ import annotations

from pathlib import Path

from engine import book
from engine.ai_client import AiClient
from engine.save_io import load_save, write_save
from engine.turn_runner import process_issue
from tests.test_turn_runner import REPO, FakeGhApi, all_normal, body_from, make_issue, make_root

ROOT = Path(__file__).resolve().parent.parent
BOOK_BODY = "### 確認\n\n旅の書を編む\n"


def _ai() -> AiClient:
    return AiClient(mock=True, fixtures_dir=ROOT / "fixtures/ai")


def _play(root: Path, gh: FakeGhApi, issue_no: int, free: str = "") -> None:
    process_issue(
        make_issue(issue_no, body_from(all_normal(), free_text=free)),
        REPO, str(root), do_git=False, gh=gh, ai=_ai(),
    )


# ---- 純粋関数 ------------------------------------------------------------


def test_stale_detection_by_source_hash():
    src = "記録A"
    narrated = book.narrated_text("章題", "語り", src)
    assert not book.is_stale(narrated, src)
    assert book.is_stale(narrated, "記録A + 続き")  # 章が伸びたら編み直す
    assert book.is_stale("", src)


def test_trim_source_keeps_both_ends():
    text = "頭" * 100 + "尾" * 100
    trimmed = book.trim_source(text, 60)
    assert trimmed.startswith("頭") and trimmed.endswith("尾")
    assert "中略" in trimmed
    assert book.trim_source("短い", 60) == "短い"


def test_assemble_puts_everything_in_order():
    text = book.assemble(
        {"title": "旅の書", "preface": "はじめに", "epilogue": "おわりに"},
        ["## 第1章\n\n本文1", "## 第2章\n\n本文2"],
        [{"name": "星穿ち", "desc": "貫く一撃"}],
        ["旅が始まった。"],
    )
    order = [text.index(x) for x in ("はじめに", "本文1", "本文2", "おわりに", "星穿ち", "旅が始まった。")]
    assert order == sorted(order)
    assert text.startswith("# 旅の書")


# ---- ランナー統合 --------------------------------------------------------


def test_book_is_compiled_from_the_chronicle(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    _play(root, gh, 1, free="フルオート 3")
    process_issue(
        make_issue(2, BOOK_BODY, title="[BOOK] 旅の書"), REPO, str(root), do_git=False, gh=gh, ai=_ai()
    )
    text = (root / book.BOOK_PATH).read_text(encoding="utf-8")
    assert "砕けた星をめぐる旅" in text  # モックの表題
    assert "墜ちた欠片の森で" in text  # モックの章題
    assert "## 年表" in text  # log.md の1行サマリも綴じられる
    assert "旅の書 — 編纂" in gh.comments[-1][1]


def test_narrated_chapters_are_cached(tmp_path):
    """一度編んだ章は編み直さない(章が増えても実行時間が伸びない)。"""
    root = make_root(tmp_path)
    gh = FakeGhApi()
    _play(root, gh, 1)
    process_issue(make_issue(2, BOOK_BODY, title="[BOOK] 旅の書"), REPO, str(root), do_git=False, gh=gh, ai=_ai())
    narrated = sorted((root / book.NARRATED_DIR).glob("*.md"))
    assert len(narrated) == 1
    stamp = narrated[0].read_text(encoding="utf-8")

    process_issue(make_issue(3, BOOK_BODY, title="[BOOK] 旅の書"), REPO, str(root), do_git=False, gh=gh, ai=_ai())
    assert narrated[0].read_text(encoding="utf-8") == stamp  # 編み直されていない
    assert "今回0章を新たに編みました" in gh.comments[-1][1]


def test_extended_chapter_is_recompiled(tmp_path):
    """戦闘が続いて章が伸びたら、その章だけ編み直す。"""
    root = make_root(tmp_path)
    gh = FakeGhApi()
    _play(root, gh, 1)
    process_issue(make_issue(2, BOOK_BODY, title="[BOOK] 旅の書"), REPO, str(root), do_git=False, gh=gh, ai=_ai())
    _play(root, gh, 3)  # 同じ戦闘のターンが増える
    process_issue(make_issue(4, BOOK_BODY, title="[BOOK] 旅の書"), REPO, str(root), do_git=False, gh=gh, ai=_ai())
    assert "今回1章を新たに編みました" in gh.comments[-1][1]


def test_book_without_ai_keeps_the_record(tmp_path):
    """AIが使えなくても記録そのものを収めて欠落を作らない。"""
    root = make_root(tmp_path)
    gh = FakeGhApi()
    _play(root, gh, 1)
    empty = tmp_path / "no_fixtures"
    empty.mkdir()
    broken = AiClient(mock=True, fixtures_dir=empty)
    process_issue(
        make_issue(2, BOOK_BODY, title="[BOOK] 旅の書"), REPO, str(root), do_git=False, gh=gh, ai=broken
    )
    text = (root / book.BOOK_PATH).read_text(encoding="utf-8")
    assert "まだ編纂されていません" in text
    assert "—— ターン1 ——" in text  # 記録は失われない


def test_book_without_any_chronicle_is_rejected(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    process_issue(
        make_issue(1, BOOK_BODY, title="[BOOK] 旅の書"), REPO, str(root), do_git=False, gh=gh, ai=_ai()
    )
    assert "まだ綴じる記録がありません" in gh.comments[-1][1]
    assert not (root / book.BOOK_PATH).exists()


def test_book_does_not_change_the_save(tmp_path):
    """編纂は記録を読むだけの行為。冒険の状態は動かさない。"""
    root = make_root(tmp_path)
    gh = FakeGhApi()
    _play(root, gh, 1)
    before = load_save(root / "save")
    process_issue(make_issue(2, BOOK_BODY, title="[BOOK] 旅の書"), REPO, str(root), do_git=False, gh=gh, ai=_ai())
    after = load_save(root / "save")
    assert after.battle.turn == before.battle.turn
    assert after.spell_tokens == before.spell_tokens
    assert after.battle.enemies[0].hp == before.battle.enemies[0].hp
