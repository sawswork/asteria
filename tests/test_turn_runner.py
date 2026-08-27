"""turn_runner のローカルE2E(git・GitHub API・AIなし)。

受入基準の検証:
- フォームから3ターン以上連続で戦い勝利できる
- フォーム連投(同一Issue再処理・不正手)でもセーブが壊れない
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from engine.commands import ROLE_LABELS
from engine.save_io import load_save, new_save, write_save
from engine.turn_runner import process_issue_event

ROOT = Path(__file__).resolve().parent.parent
REPO = "owner/repo"
OWNER = "owner"


def make_root(tmp_path: Path, seed: int = 999) -> Path:
    (tmp_path / "world").mkdir()
    for name in ("world.json", "balance.json"):
        shutil.copy(ROOT / "world" / name, tmp_path / "world" / name)
    world = json.loads((tmp_path / "world/world.json").read_text(encoding="utf-8"))
    balance = json.loads((tmp_path / "world/balance.json").read_text(encoding="utf-8"))
    write_save(new_save(world, balance, seed=seed), tmp_path / "save/state.json")
    return tmp_path


def body_from(commands: dict[str, tuple[str, str]], free_text: str = "") -> str:
    parts: list[str] = []
    for role, label in ROLE_LABELS.items():
        action, target = commands[role]
        parts.append(f"### {label}の行動\n\n{action}\n")
        parts.append(f"### {label}の対象\n\n{target}\n")
    parts.append(f"### 自由記述(任意・M2で対応)\n\n{free_text or '_No response_'}\n")
    return "\n".join(parts)


def make_event(number: int, body: str, author: str = OWNER, title: str = "[TURN] ターン入力") -> dict:
    return {"issue": {"number": number, "title": title, "body": body, "user": {"login": author}}}


def run(root: Path, event: dict) -> int:
    return process_issue_event(event, REPO, str(root), do_git=False, gh=None)


def _policy_commands(root: Path) -> dict[str, tuple[str, str]]:
    """セーブを読んで妥当な手を選ぶ簡易プレイヤー方針。"""
    save = load_save(root / "save/state.json")

    def ready(role: str, idx: int) -> bool:
        return save.member_by_role(role).abilities[idx].ready_in == 0

    def gauge_full(role: str) -> bool:
        return save.member_by_role(role).ult_gauge >= 100

    hurt = min(m.hp / m.max_hp for m in save.party if m.alive)
    return {
        "attacker": ("奥義", "自動") if gauge_full("attacker") else (("アビ1", "自動") if ready("attacker", 0) else ("通常攻撃", "自動")),
        "support": ("アビ1", "自動") if ready("support", 0) else ("通常攻撃", "自動"),
        "tank": ("アビ2", "自動") if ready("tank", 1) else ("通常攻撃", "自動"),
        "healer": ("アビ1", "自動") if (hurt < 0.7 and ready("healer", 0)) else ("通常攻撃", "自動"),
    }


def test_e2e_win_battle_from_forms(tmp_path):
    root = make_root(tmp_path)
    turns = 0
    result = None
    for number in range(1, 21):
        run(root, make_event(number, body_from(_policy_commands(root))))
        save = load_save(root / "save/state.json")
        assert number in save.processed_issues
        turns += 1
        assert save.battle is not None
        if save.battle.result:
            result = save.battle.result
            break
    assert result == "victory"
    assert turns >= 3  # 受入: 3ターン以上連続で戦い勝利
    assert (root / "assets/board.svg").exists()
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "assets/board.svg" in readme
    # 勝利後にもう1ターン送ると新しい戦闘が始まる
    run(root, make_event(99, body_from(_policy_commands(root))))
    save = load_save(root / "save/state.json")
    assert save.battle.active and save.battle.turn == 2


def test_duplicate_issue_is_idempotent(tmp_path):
    root = make_root(tmp_path)
    event = make_event(1, body_from(_policy_commands(root)))
    run(root, event)
    snapshot = (root / "save/state.json").read_bytes()
    run(root, event)  # 同じIssueをもう一度
    assert (root / "save/state.json").read_bytes() == snapshot


def test_invalid_move_does_not_consume_turn(tmp_path):
    root = make_root(tmp_path)
    run(root, make_event(1, body_from({r: ("アビ1", "自動") if r == "attacker" else ("通常攻撃", "自動") for r in ROLE_LABELS})))
    snapshot = (root / "save/state.json").read_bytes()
    save = load_save(root / "save/state.json")
    assert save.member_by_role("attacker").abilities[0].ready_in > 0
    # CT中のアビ1をもう一度 → 不正手 → セーブ不変・ターン不消費
    run(root, make_event(2, body_from({r: ("アビ1", "自動") if r == "attacker" else ("通常攻撃", "自動") for r in ROLE_LABELS})))
    assert (root / "save/state.json").read_bytes() == snapshot
    save2 = load_save(root / "save/state.json")
    assert 2 not in save2.processed_issues
    assert save2.battle.turn == save.battle.turn


def test_non_owner_is_ignored(tmp_path):
    root = make_root(tmp_path)
    snapshot = (root / "save/state.json").read_bytes()
    run(root, make_event(1, body_from(_policy_commands(root)), author="mallory"))
    assert (root / "save/state.json").read_bytes() == snapshot
    assert not (root / "assets/board.svg").exists()


def test_wrong_title_is_ignored(tmp_path):
    root = make_root(tmp_path)
    snapshot = (root / "save/state.json").read_bytes()
    run(root, make_event(1, body_from(_policy_commands(root)), title="ふつうのバグ報告"))
    assert (root / "save/state.json").read_bytes() == snapshot


def test_broken_body_is_invalid_not_crash(tmp_path):
    root = make_root(tmp_path)
    snapshot = (root / "save/state.json").read_bytes()
    run(root, make_event(1, "こんにちは"))
    assert (root / "save/state.json").read_bytes() == snapshot
