"""年代記を1冊の書物へ編む。

素材は3つ:
  save/chronicle/chapter-NNN.md  出来事の全文(本文)
  save/spells/*.json             紡いだ技のすべて(魔導書)
  save/log.md                    節目の1行サマリ(目次)

編纂は章ごとに1回のAI呼び出しで行い、結果を book/chapters/ にキャッシュする。
章が増えても1回の実行時間が伸びないようにするためで、続きは再実行で編める。
素材が変わった章(戦闘の続き等)はハッシュ不一致で自動的に編み直される。

ここは文字列→文字列の純粋関数のみ。AI呼び出しとファイルI/Oは呼び出し側の責務。
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

BOOK_DIR = "book"
NARRATED_DIR = "book/chapters"
BOOK_PATH = "book/journey.md"
_SRC_SHA = re.compile(r"<!-- src-sha:\s*([0-9a-f]+)\s*-->")


def source_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_stale(narrated: str, source: str) -> bool:
    """編纂済みの章が素材と食い違っていないか(戦闘が続いて章が伸びた場合など)。"""
    found = _SRC_SHA.search(narrated or "")
    return not found or found.group(1) != source_sha(source)


def narrated_text(title: str, text: str, source: str) -> str:
    """編纂した章の保存形式(素材のハッシュを添えて再編纂の要否を判定できるようにする)。"""
    return f"<!-- src-sha: {source_sha(source)} -->\n## {title}\n\n{text.strip()}\n"


def strip_marker(narrated: str) -> str:
    return _SRC_SHA.sub("", narrated).strip()


def trim_source(text: str, limit: int) -> str:
    """AIへ渡す素材の長さを抑える。切る時は中央を落として冒頭と結末を残す。"""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n\n(…中略…)\n\n{tail}"


def raw_chapter(title: str, source: str) -> str:
    """未編纂の章(AIが間に合わなかった分)。記録そのものを載せて欠落を作らない。"""
    return f"## {title}\n\n*(この章はまだ編纂されていません。記録のまま収めます)*\n\n{source.strip()}\n"


def assemble(
    frame: dict[str, str], chapters: list[str], spells: list[dict[str, Any]], journal: list[str]
) -> str:
    """表題・序文・各章・魔導書・年表を1冊にまとめる。"""
    parts = [f"# {frame.get('title', '旅の書')}", ""]
    if frame.get("preface"):
        parts += [frame["preface"].strip(), "", "---", ""]
    for chapter in chapters:
        parts += [chapter.strip(), "", "---", ""]
    if frame.get("epilogue"):
        parts += ["## 終章", "", frame["epilogue"].strip(), "", "---", ""]
    if spells:
        parts += ["## 魔導書 — この旅で紡がれた技", ""]
        for sp in spells:
            desc = str(sp.get("desc", "")).strip()
            parts.append(f"- **{sp.get('name', '?')}** — {desc}")
        parts.append("")
    if journal:
        parts += ["## 年表", ""] + [f"- {line}" for line in journal] + [""]
    return "\n".join(parts).rstrip() + "\n"
