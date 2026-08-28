"""過去のIssueコメントから年代記を復元する。

年代記(`save/chronicle/`)は途中から導入したため、それ以前の冒険の本文は
リポジトリに残っていない。だがエンジンの返信コメントには全文が載っており、
Issueは消えずに残る。ここではその返信を読み直して章を組み直す。

解析はネットワークに触れない純粋関数にしてある(取得は gh_api の責務)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import chronicle

# エンジンが返信に使う目印(いずれも engine 自身の語彙。世界の固有名詞ではない)
BATTLE_START = "新しい戦いが始まった: **"
SKIP_PREFIXES = ("## ⚠", "ℹ ")
_CODE_BLOCK = re.compile(r"```\n(.*?)\n```", re.DOTALL)
_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_LINKS = re.compile(r"^📺 .*$", re.MULTILINE)
_INTRO = re.compile(r"^>\s*(.+)$", re.MULTILINE)


@dataclass
class Reply:
    """復元の入力: 1つのIssueとエンジンの返信本文。"""

    number: int
    title: str
    body: str


def _clean(body: str) -> str:
    """返信からナビゲーションリンクを落とす(本にリンクは要らない)。"""
    return _LINKS.sub("", body).strip()


def is_recordable(reply: Reply) -> bool:
    """記録に値する返信か(不正手やスキップ通知は冒険の出来事ではない)。"""
    body = reply.body.lstrip()
    return bool(body) and not body.startswith(SKIP_PREFIXES)


def battle_name(body: str) -> str:
    """返信が新しい戦いの始まりを告げていれば、その戦闘名。無ければ空。"""
    if BATTLE_START not in body:
        return ""
    tail = body.split(BATTLE_START, 1)[1]
    return tail.split("**", 1)[0].strip()


def _entry_for(reply: Reply) -> tuple[str, str]:
    """(見出し, 本文)。ターン結果はログのコードブロックを、儀式は本文をそのまま残す。"""
    body = _clean(reply.body)
    headings = _HEADING.findall(body)
    heading = headings[0] if headings else reply.title
    heading = heading.replace("⚔ ", "").replace(" の結果", "の結果")
    blocks = _CODE_BLOCK.findall(body)
    if blocks:  # 戦闘ターン: ログ本文だけを残す
        return f"{heading}(Issue #{reply.number})", "```\n" + blocks[0].strip() + "\n```"
    # 儀式(技生成・アップデート・時戻し): 見出し行を除いた本文を残す
    text = _HEADING.sub("", body, count=1).strip()
    return f"{heading}(Issue #{reply.number})", text


def rebuild(replies: list[Reply]) -> dict[int, str]:
    """Issue番号昇順の返信列から {章番号: 章の本文} を組み立てる。

    章は戦闘単位。「新しい戦いが始まった」で章が進み、その後の儀式は同じ章に入る
    (=年代記の chapter_number と同じ数え方)。
    """
    chapters: dict[int, str] = {}
    chapter = 0
    for reply in sorted(replies, key=lambda r: r.number):
        if not is_recordable(reply):
            continue
        name = battle_name(reply.body)
        if name:
            chapter += 1
            intro = ""
            after = reply.body.split(BATTLE_START, 1)[1]
            found = _INTRO.search(after)
            if found:
                intro = found.group(1).strip()
            chapters[chapter] = _header(chapter, name, intro)
        if chapter == 0:  # 最初の戦いより前の出来事は第1章の前置きにまとめる
            chapter = 1
            chapters.setdefault(chapter, _header(chapter, "旅立ち", ""))
        heading, body = _entry_for(reply)
        chapters[chapter] = chronicle.append_entry(chapters[chapter], reply.number, heading, body)
    return chapters


def _header(chapter: int, name: str, intro: str) -> str:
    lines = [f"# 第{chapter}章 {name}", ""]
    if intro:
        lines += [f"> {intro}", ""]
    lines += ["*(この章は Issue の返信から復元した記録です)*", ""]
    return "\n".join(lines)


def write_chapters(root: Any, chapters: dict[int, str]) -> list[str]:
    """章を save/chronicle/ へ書き出す。既存ファイルは上書きしない(実記録を優先)。"""
    from pathlib import Path

    out_dir = Path(root) / "save" / chronicle.CHRONICLE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for number, text in sorted(chapters.items()):
        path = out_dir / chronicle.chapter_filename(number)
        if path.exists():
            continue  # 実際に記録された章の方が正確なので触らない
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        written.append(path.name)
    return written
