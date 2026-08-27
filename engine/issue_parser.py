"""Issue Form 本文(Markdown)→ ターンコマンド。

Issue Forms は本文を「### <ラベル>\n\n<値>」の並びでレンダリングする。
ラベルは .github/ISSUE_TEMPLATE/turn.yml と一致させること(スロット語彙は不変)。

防御的仕様:
- 区切りとして扱う見出しは既知のフィールドラベルのみ。同じラベルの重複は初出を優先する
  (自由記述欄に「### タンクの行動」等を書き込んでもドロップダウンの選択を上書きできない)
- 自由記述欄はフォーム末尾のフィールドなので、その見出し以降は全て自由記述の内容として扱う
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .commands import ROLE_LABELS, Command

FREE_TEXT_LABEL = "自由記述"
NO_RESPONSE = "_No response_"

COMMAND_LABELS = frozenset(
    f"{label}の{kind}" for label in ROLE_LABELS.values() for kind in ("行動", "対象")
)


@dataclass
class ParsedTurn:
    commands: dict[str, Command] = field(default_factory=dict)  # role -> Command
    free_text: str = ""
    errors: list[str] = field(default_factory=list)


def _sections(body: str) -> tuple[dict[str, str], str]:
    """既知ラベルの見出し→値のマップと、自由記述の内容を返す。"""
    sections: dict[str, str] = {}
    free_text_lines: list[str] | None = None
    current: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal current, buf
        if current is not None:
            sections.setdefault(current, "\n".join(buf).strip())  # 初出優先
        current = None
        buf = []

    for raw_line in body.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        if free_text_lines is not None:
            free_text_lines.append(line)
            continue
        if line.startswith("### "):
            heading = line[4:].strip()
            if heading.startswith(FREE_TEXT_LABEL):
                flush()
                free_text_lines = []
                continue
            if heading in COMMAND_LABELS:
                flush()
                current = heading
                continue
            # 未知の見出しは区切りとして扱わず、現在のセクションの内容とみなす
        if current is not None:
            buf.append(line)
    flush()

    free_text = "\n".join(free_text_lines).strip() if free_text_lines is not None else ""
    if free_text == NO_RESPONSE:
        free_text = ""
    return sections, free_text


def parse_issue_body(body: str) -> ParsedTurn:
    parsed = ParsedTurn()
    sections, parsed.free_text = _sections(body)
    for role, label in ROLE_LABELS.items():
        action = sections.get(f"{label}の行動", "").strip()
        target = sections.get(f"{label}の対象", "").strip()
        if not action or action == NO_RESPONSE:
            parsed.errors.append(f"「{label}の行動」が未入力です")
            continue
        if not target or target == NO_RESPONSE:
            target = "自動"
        parsed.commands[role] = Command(role=role, action=action, target=target)
    return parsed
