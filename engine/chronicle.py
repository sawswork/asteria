"""年代記(chronicle): 冒険の出来事を全文で残す。

`save/log.md`(旅の記憶)が1行サマリの目次なのに対し、こちらは**本文**を残す。
最後に1冊の書籍へ編むための素材なので、要約せず・上限を設けずそのまま積む。

章立ては戦闘単位。1つの章に「その戦いの全ターン」と「その後の拠点での出来事
(技生成・アップデート・時戻し)」が時系列で入る。

追記は**Issue番号をマーカーにした冪等な置換**で行う。push競合のリプレイでは
同じIssueが何度も再解決されるため、素朴な追記だと本文が二重になる。
この方式なら何度処理しても、そのIssueのブロックは常に1つだけになる。

I/Oは呼び出し側(turn_runner)の責務。ここは文字列→文字列の純粋関数のみ。
"""
from __future__ import annotations

import re
from typing import Any

CHRONICLE_DIR = "chronicle"
MARKER = "<!-- issue:{n} -->"


def chapter_filename(chapter: int) -> str:
    return f"chapter-{max(1, int(chapter)):03d}.md"


def _marker(issue_number: int) -> str:
    return MARKER.format(n=int(issue_number))


def _entry_pattern(issue_number: int) -> re.Pattern[str]:
    marker = re.escape(_marker(issue_number))
    # マーカーから次のマーカーまで(または末尾まで)が1ブロック
    return re.compile(rf"{marker}\n.*?(?=<!-- issue:\d+ -->\n|\Z)", re.DOTALL)


def append_entry(text: str, issue_number: int, heading: str, body: str) -> str:
    """Issue1件分の出来事を章へ書き込む。

    同じIssueが再処理された場合は**その場で置換**する(末尾へ移すと時系列が崩れるため)。
    未登場のIssueなら末尾へ追記する。
    """
    block = f"{_marker(issue_number)}\n## {heading}\n\n{body.rstrip()}\n\n"
    pattern = _entry_pattern(issue_number)
    if pattern.search(text):
        return pattern.sub(lambda _m: block, text, count=1)
    return (text.rstrip() + "\n\n" + block) if text.strip() else block


def chapter_header(chapter: int, battle_name: str, intro: str, enemies: list[Any], party: list[Any]) -> str:
    """章の冒頭(戦いの舞台紹介)。章ファイルが無い時だけ書く。"""
    lines = [f"# 第{chapter}章 {battle_name}", ""]
    if intro:
        lines += [f"> {intro}", ""]
    for e in enemies:
        title = f"({e.title})" if getattr(e, "title", "") else ""
        lines.append(f"- **敵** {e.name}{title} — ランク {e.tier} / HP {e.max_hp} / 攻 {e.atk} 防 {e.df} 速 {e.agi}")
    roster = " / ".join(f"{m.name} HP{m.max_hp}" for m in party)
    lines += [f"- **一党** {roster}", ""]
    return "\n".join(lines)


def turn_entry(turn_label: str, issue_number: int, log_lines: list[str]) -> tuple[str, str]:
    """戦闘ターンの記録。(見出し, 本文) を返す。"""
    body = "```\n" + "\n".join(log_lines) + "\n```"
    return f"{turn_label}(Issue #{issue_number})", body


def ritual_entry(
    issue_number: int, title: str, detail_lines: list[str], quote: str = "", quote_label: str = ""
) -> tuple[str, str]:
    """拠点での出来事(技生成・アップデート・時戻し)の記録。"""
    parts: list[str] = []
    if quote:
        if quote_label:
            parts.append(f"**{quote_label}**")
        parts += [f"> {line}" for line in quote.splitlines() if line.strip()]
        parts.append("")
    parts += detail_lines
    return f"{title}(Issue #{issue_number})", "\n".join(parts)


def outcome_entry(result: str, battle_name: str, turn_no: int) -> str:
    """章の締め(勝敗)。本文の末尾に置く。"""
    verdict = {"victory": "勝利", "defeat": "敗北"}.get(result, result)
    return f"\n---\n\n**幕引き**: 「{battle_name}」に{verdict}(ターン{turn_no})\n"
