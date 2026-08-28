"""Issue Form 本文(Markdown)→ 各フォームの入力。

Issue Forms は本文を「### <ラベル>\n\n<値>」の並びでレンダリングする。
ラベルは .github/ISSUE_TEMPLATE/*.yml と一致させること(スロット語彙は不変)。

防御的仕様:
- 区切りとして扱う見出しは既知のフィールドラベルのみ。同じラベルの重複は初出を優先する
  (自由記述欄に「### タンクの行動」等を書き込んでもドロップダウンの選択を上書きできない)
- 末尾の自由記述系フィールド(自由記述/詠唱文/方向性)は、その見出し以降を全て内容として扱う
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .commands import ROLE_LABELS, Command

NO_RESPONSE = "_No response_"

FREE_TEXT_LABEL = "自由記述"
INCANTATION_LABEL = "詠唱文"
DIRECTION_LABEL = "方向性"
OATH_LABEL = "誓約"

_CHECKED_RE = re.compile(r"^-\s*\[[xX]\]\s*(.+)$")  # チェック済みのcheckbox行

COMMAND_LABELS = frozenset(
    f"{label}の{kind}" for label in ROLE_LABELS.values() for kind in ("行動", "対象")
)
MEMBER_LABEL = "対象メンバー"
SLOT_LABEL = "スロット"
CHOICE_LABEL = "選択"

SLOT_VALUES = ("アビ1", "アビ2", "アビ3", "奥義")
CHOICE_VIEW = "提案を見る"
CHOICE_VALUES = (CHOICE_VIEW, "案1", "案2", "案3")


def _sections(
    body: str, known_labels: frozenset[str], free_prefix: str
) -> tuple[dict[str, str], str]:
    """既知ラベルの見出し→値のマップと、末尾自由記述の内容を返す。"""
    sections: dict[str, str] = {}
    free_lines: list[str] | None = None
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
        if free_lines is not None:
            free_lines.append(line)
            continue
        if line.startswith("### "):
            heading = line[4:].strip()
            if free_prefix and heading.startswith(free_prefix):
                flush()
                free_lines = []
                continue
            if heading in known_labels:
                flush()
                current = heading
                continue
            # 未知の見出しは区切りとして扱わず、現在のセクションの内容とみなす
        if current is not None:
            buf.append(line)
    flush()

    free_text = "\n".join(free_lines).strip() if free_lines is not None else ""
    if free_text == NO_RESPONSE:
        free_text = ""
    return sections, free_text[:500]  # AIプロンプトへ渡す自由記述は長さを制限する


# ---- ターン入力 ----------------------------------------------------------


@dataclass
class ParsedTurn:
    commands: dict[str, Command] = field(default_factory=dict)  # role -> Command
    free_text: str = ""
    errors: list[str] = field(default_factory=list)


def parse_issue_body(body: str) -> ParsedTurn:
    parsed = ParsedTurn()
    sections, parsed.free_text = _sections(body, COMMAND_LABELS, FREE_TEXT_LABEL)
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


# ---- 技生成フォーム ------------------------------------------------------


@dataclass
class ParsedGenerate:
    member_role: str = ""
    slot: str = ""
    incantation: str = ""
    oath_labels: list[str] = field(default_factory=list)  # チェックされた誓約の表示文言
    errors: list[str] = field(default_factory=list)


def parse_generate_body(body: str) -> ParsedGenerate:
    parsed = ParsedGenerate()
    known = frozenset({MEMBER_LABEL, SLOT_LABEL, OATH_LABEL})
    sections, parsed.incantation = _sections(body, known, INCANTATION_LABEL)
    for line in sections.get(OATH_LABEL, "").splitlines():
        m = _CHECKED_RE.match(line.strip())
        if m:
            parsed.oath_labels.append(m.group(1).strip())
    member_label = sections.get(MEMBER_LABEL, "").strip()
    role = {v: k for k, v in ROLE_LABELS.items()}.get(member_label, "")
    if not role:
        parsed.errors.append(f"「{MEMBER_LABEL}」が不正です: {member_label or '(未入力)'}")
    parsed.member_role = role
    slot = sections.get(SLOT_LABEL, "").strip()
    if slot not in SLOT_VALUES:
        parsed.errors.append(f"「{SLOT_LABEL}」が不正です: {slot or '(未入力)'}")
    parsed.slot = slot
    if not parsed.incantation.strip():
        parsed.errors.append("「詠唱文」が未入力です(どんな技にしたいか自由に書いてください)")
    return parsed


# ---- 技アップデートフォーム ----------------------------------------------


@dataclass
class ParsedUpdate:
    member_role: str = ""
    slot: str = ""
    choice: str = ""
    direction: str = ""
    errors: list[str] = field(default_factory=list)


def parse_update_body(body: str) -> ParsedUpdate:
    parsed = ParsedUpdate()
    known = frozenset({MEMBER_LABEL, SLOT_LABEL, CHOICE_LABEL})
    sections, parsed.direction = _sections(body, known, DIRECTION_LABEL)
    member_label = sections.get(MEMBER_LABEL, "").strip()
    role = {v: k for k, v in ROLE_LABELS.items()}.get(member_label, "")
    if not role:
        parsed.errors.append(f"「{MEMBER_LABEL}」が不正です: {member_label or '(未入力)'}")
    parsed.member_role = role
    slot = sections.get(SLOT_LABEL, "").strip()
    if slot not in SLOT_VALUES:
        parsed.errors.append(f"「{SLOT_LABEL}」が不正です: {slot or '(未入力)'}")
    parsed.slot = slot
    choice = sections.get(CHOICE_LABEL, "").strip()
    if choice not in CHOICE_VALUES:
        parsed.errors.append(f"「{CHOICE_LABEL}」が不正です: {choice or '(未入力)'}")
    parsed.choice = choice
    return parsed
