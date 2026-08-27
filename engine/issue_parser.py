"""Issue Form 本文(Markdown)→ ターンコマンド。

Issue Forms は本文を「### <ラベル>\n\n<値>」の並びでレンダリングする。
ラベルは .github/ISSUE_TEMPLATE/turn.yml と一致させること(スロット語彙は不変)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .commands import ROLE_LABELS, Command

FREE_TEXT_LABEL = "自由記述"
NO_RESPONSE = "_No response_"


@dataclass
class ParsedTurn:
    commands: dict[str, Command] = field(default_factory=dict)  # role -> Command
    free_text: str = ""
    errors: list[str] = field(default_factory=list)


def _sections(body: str) -> dict[str, str]:
    """「### 見出し」→本文 のマップ。値は前後空白を除去した1ブロック。"""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for raw_line in body.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        if line.startswith("### "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[4:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def parse_issue_body(body: str) -> ParsedTurn:
    parsed = ParsedTurn()
    sections = _sections(body)
    for role, label in ROLE_LABELS.items():
        action = sections.get(f"{label}の行動", "").strip()
        target = sections.get(f"{label}の対象", "").strip()
        if not action or action == NO_RESPONSE:
            parsed.errors.append(f"「{label}の行動」が未入力です")
            continue
        if not target or target == NO_RESPONSE:
            target = "自動"
        parsed.commands[role] = Command(role=role, action=action, target=target)
    for key, value in sections.items():
        if key.startswith(FREE_TEXT_LABEL) and value and value != NO_RESPONSE:
            parsed.free_text = value
    return parsed
