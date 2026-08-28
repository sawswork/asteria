"""過去のIssue返信から年代記を復元する処理のテスト(実際の返信形式を模す)。"""
from __future__ import annotations

from engine import backfill, chronicle

TURN1 = """## ⚔ ターン1の結果

新しい戦いが始まった: **星蝕の仔狼との遭遇**

> 夜空の欠片が墜ちた森で、星を喰らう仔狼が唸りを上げた。

```
—— ターン1 ——
ソラの星技「星走り」! 星蝕の仔狼に21ダメージ!
```

**敵の状態**: 星蝕の仔狼 HP 213/240

📺 [最新の戦況ボードを見る](https://example.com) / ▶ [次のターン](https://example.com)"""

TURN2 = """## ⚔ ターン2の結果

```
—— ターン2 ——
ソラの攻撃! 星蝕の仔狼に11ダメージ!
```

**敵の状態**: 星蝕の仔狼 HP 202/240

📺 [戦況ボード](https://example.com)"""

INVALID = """## ⚠ 不正な手が含まれています

- **attacker**: アビ1「星走り」はCT中(あと1ターン)

**ターンは消費されていません。**"""

GENERATE = """## ✨ 技生成の儀式 — 完了

ミオの**アビ3**が「光芒の矢」から生まれ変わった:

**星の雫**(CT2)

📺 [戦況ボード](https://example.com)"""

TURN_NEW_BATTLE = """## ⚔ ターン1の結果

新しい戦いが始まった: **夜哭きの梟との戦い**

```
—— ターン1 ——
ソラの攻撃! 夜哭きの梟に8ダメージ!
```

📺 [戦況ボード](https://example.com)"""


def _replies(*pairs) -> list[backfill.Reply]:
    return [backfill.Reply(number=n, title="[TURN] ターン入力", body=b) for n, b in pairs]


def test_battle_start_opens_a_new_chapter():
    chapters = backfill.rebuild(_replies((1, TURN1), (2, TURN2), (3, TURN_NEW_BATTLE)))
    assert sorted(chapters) == [1, 2]
    assert "# 第1章 星蝕の仔狼との遭遇" in chapters[1]
    assert "夜空の欠片が墜ちた森で" in chapters[1]  # 登場ログも拾う
    assert "# 第2章 夜哭きの梟との戦い" in chapters[2]


def test_turns_are_grouped_into_their_battle():
    chapters = backfill.rebuild(_replies((1, TURN1), (2, TURN2), (3, TURN_NEW_BATTLE)))
    assert "—— ターン1 ——" in chapters[1] and "—— ターン2 ——" in chapters[1]
    assert "—— ターン2 ——" not in chapters[2]


def test_invalid_moves_are_not_recorded():
    """不正手はターンを消費しない=冒険の出来事ではないので本には載せない。"""
    chapters = backfill.rebuild(_replies((1, TURN1), (2, INVALID)))
    assert "不正な手" not in chapters[1]
    assert chapters[1].count("<!-- issue:") == 1


def test_rituals_join_the_current_chapter():
    chapters = backfill.rebuild(_replies((1, TURN1), (2, GENERATE)))
    assert "技生成の儀式" in chapters[1]
    assert "星の雫" in chapters[1]


def test_navigation_links_are_dropped():
    chapters = backfill.rebuild(_replies((1, TURN1)))
    assert "https://example.com" not in chapters[1]


def test_entries_are_ordered_by_issue_number():
    chapters = backfill.rebuild(_replies((2, TURN2), (1, TURN1)))  # 順不同で渡す
    assert chapters[1].index("<!-- issue:1 -->") < chapters[1].index("<!-- issue:2 -->")


def test_events_before_any_battle_go_into_chapter_one():
    chapters = backfill.rebuild(_replies((1, GENERATE)))
    assert 1 in chapters and "技生成の儀式" in chapters[1]


def test_existing_chapters_are_never_overwritten(tmp_path):
    """実際に記録された章の方が正確なので、復元は空いている章だけ埋める。"""
    out = tmp_path / "save" / chronicle.CHRONICLE_DIR
    out.mkdir(parents=True)
    kept = out / chronicle.chapter_filename(1)
    kept.write_text("実記録", encoding="utf-8")
    written = backfill.write_chapters(tmp_path, {1: "復元", 2: "復元2"})
    assert written == [chronicle.chapter_filename(2)]
    assert kept.read_text(encoding="utf-8") == "実記録"
