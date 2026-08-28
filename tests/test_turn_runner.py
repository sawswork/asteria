"""turn_runner のローカルE2E(実AI・実GitHub APIなし。gitはローカルbareリポジトリで統合テスト)。

受入基準の検証:
- フォームから3ターン以上連続で戦い勝利できる
- フォーム連投(同一Issue再処理・不正手)でもセーブが壊れない
- push競合時はリモート状態からのリプレイで正しく復旧する
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from engine.commands import ROLE_LABELS
from engine.save_io import load_save, new_save, write_save
from engine.turn_runner import process_issue

ROOT = Path(__file__).resolve().parent.parent
REPO = "owner/repo"
OWNER = "owner"


def _snapshot(root: Path) -> bytes:
    """save/ 配下全ファイルの決定的スナップショット(冪等性の検証用)。"""
    parts: list[bytes] = []
    for f in sorted((root / "save").rglob("*")):
        if f.is_file():
            parts.append(str(f.relative_to(root)).encode() + b":" + f.read_bytes())
    return b"".join(parts)


def make_root(target: Path, seed: int = 999) -> Path:
    (target / "world").mkdir(parents=True)
    for name in ("world.json", "balance.json"):
        shutil.copy(ROOT / "world" / name, target / "world" / name)
    world = json.loads((target / "world/world.json").read_text(encoding="utf-8"))
    balance = json.loads((target / "world/balance.json").read_text(encoding="utf-8"))
    write_save(new_save(world, balance, seed=seed), target / "save")
    return target


def body_from(commands: dict[str, tuple[str, str]], free_text: str = "") -> str:
    parts: list[str] = []
    for role, label in ROLE_LABELS.items():
        action, target = commands[role]
        parts.append(f"### {label}の行動\n\n{action}\n")
        parts.append(f"### {label}の対象\n\n{target}\n")
    parts.append(f"### 自由記述(任意・M2で対応)\n\n{free_text or '_No response_'}\n")
    return "\n".join(parts)


def make_issue(number: int, body: str, author: str = OWNER, title: str = "[TURN] ターン入力") -> dict:
    return {"number": number, "title": title, "body": body, "user": {"login": author}}


def run(root: Path, issue: dict, do_git: bool = False, gh=None) -> None:
    process_issue(issue, REPO, str(root), do_git=do_git, gh=gh)


def all_normal() -> dict[str, tuple[str, str]]:
    return {role: ("通常攻撃", "自動") for role in ROLE_LABELS}


def _policy_commands(root: Path) -> dict[str, tuple[str, str]]:
    """セーブを読んで妥当な手を選ぶ簡易プレイヤー方針。"""
    save = load_save(root / "save")

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


class FakeGhApi:
    """記録専用のGhApi代替。"""

    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.labels: list[tuple[int, list[str]]] = []
        self.raise_on_comment = False

    def list_open_turn_issues(self, title_prefix: str) -> list[dict]:
        return []

    def post_comment(self, issue_number: int, body: str) -> None:
        if self.raise_on_comment:
            raise RuntimeError("comment failed")
        self.comments.append((issue_number, body))

    def close_issue(self, issue_number: int) -> None:
        self.closed.append(issue_number)

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        self.labels.append((issue_number, labels))


# ---- 受入E2E ------------------------------------------------------------


def test_e2e_win_battle_from_forms(tmp_path):
    root = make_root(tmp_path)
    turns = 0
    result = None
    for number in range(1, 21):
        run(root, make_issue(number, body_from(_policy_commands(root))))
        save = load_save(root / "save")
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
    run(root, make_issue(99, body_from(_policy_commands(root))))
    save = load_save(root / "save")
    assert save.battle.active and save.battle.turn == 2


def test_defeat_path_and_rematch(tmp_path):
    root = make_root(tmp_path)
    run(root, make_issue(1, body_from(all_normal())))  # 戦闘開始+1ターン
    save = load_save(root / "save")
    for m in save.party:
        m.hp = 1
    save.battle.enemies[0].atk = 999
    save.battle.enemies[0].hp = 9999
    write_save(save, root / "save")

    result = None
    for number in range(2, 10):
        run(root, make_issue(number, body_from(all_normal())))
        save = load_save(root / "save")
        if save.battle.result:
            result = save.battle.result
            break
    assert result == "defeat"
    assert save.stats.get("defeats") == 1
    board = (root / "assets/board.svg").read_text(encoding="utf-8")
    assert "敗北" in board
    assert "敗北" in (root / "README.md").read_text(encoding="utf-8")
    # 敗北後の次のターンで新しい戦闘が自動開始され、パーティは全快から1ターン進んでいる
    run(root, make_issue(50, body_from(all_normal())))
    save = load_save(root / "save")
    assert save.battle.active and save.battle.result is None
    assert save.battle.turn == 2
    assert save.battle.enemies[0].hp < save.battle.enemies[0].max_hp


# ---- 冪等性・ガード -----------------------------------------------------


def test_duplicate_issue_is_idempotent(tmp_path):
    root = make_root(tmp_path)
    issue = make_issue(1, body_from(_policy_commands(root)))
    run(root, issue)
    snapshot = _snapshot(root)
    run(root, issue)  # 同じIssueをもう一度
    assert _snapshot(root) == snapshot


def test_invalid_move_does_not_consume_turn(tmp_path):
    root = make_root(tmp_path)
    ct_move = {r: ("アビ1", "自動") if r == "attacker" else ("通常攻撃", "自動") for r in ROLE_LABELS}
    run(root, make_issue(1, body_from(ct_move)))
    snapshot = _snapshot(root)
    save = load_save(root / "save")
    assert save.member_by_role("attacker").abilities[0].ready_in > 0
    # CT中のアビ1をもう一度 → 不正手 → セーブ不変・ターン不消費
    run(root, make_issue(2, body_from(ct_move)))
    assert _snapshot(root) == snapshot
    save2 = load_save(root / "save")
    assert 2 not in save2.processed_issues
    assert save2.battle.turn == save.battle.turn


def test_non_owner_is_ignored(tmp_path):
    root = make_root(tmp_path)
    snapshot = _snapshot(root)
    run(root, make_issue(1, body_from(all_normal()), author="mallory"))
    assert _snapshot(root) == snapshot
    assert not (root / "assets/board.svg").exists()


def test_wrong_title_is_ignored(tmp_path):
    root = make_root(tmp_path)
    snapshot = _snapshot(root)
    run(root, make_issue(1, body_from(all_normal()), title="ふつうのバグ報告"))
    assert _snapshot(root) == snapshot


def test_broken_body_is_invalid_not_crash(tmp_path):
    root = make_root(tmp_path)
    snapshot = _snapshot(root)
    run(root, make_issue(1, "こんにちは"))
    assert _snapshot(root) == snapshot


# ---- GitHub返信経路(FakeGhApi) ----------------------------------------


def test_valid_turn_replies_labels_and_closes(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    run(root, make_issue(1, body_from(all_normal())), gh=gh)
    assert gh.closed == [1]
    assert gh.labels == [(1, ["turn"])]
    assert len(gh.comments) == 1
    number, body = gh.comments[0]
    assert number == 1 and "ターン1の結果" in body


def test_invalid_move_replies_error_and_closes_without_label(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    ct_move = {r: ("アビ1", "自動") if r == "attacker" else ("通常攻撃", "自動") for r in ROLE_LABELS}
    run(root, make_issue(1, body_from(ct_move)), gh=gh)
    run(root, make_issue(2, body_from(ct_move)), gh=gh)  # CT中 → 不正手
    assert gh.closed == [1, 2]
    assert gh.labels == [(1, ["turn"])]  # 不正手にはラベルなし
    _, error_body = gh.comments[1]
    assert "不正な手" in error_body and "ターンは消費されていません" in error_body


def test_duplicate_issue_replies_processed_notice(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    issue = make_issue(1, body_from(all_normal()))
    run(root, issue, gh=gh)
    run(root, issue, gh=gh)
    assert gh.closed == [1, 1]
    assert "処理済み" in gh.comments[1][1]


def test_comment_failure_propagates_after_turn_consumed(tmp_path):
    # 返信に失敗してもセーブは確定済み(ターン消費)で、Issueはクローズされない=再実行で案内が出る
    root = make_root(tmp_path)
    gh = FakeGhApi()
    gh.raise_on_comment = True
    with pytest.raises(RuntimeError):
        run(root, make_issue(1, body_from(all_normal())), gh=gh)
    save = load_save(root / "save")
    assert 1 in save.processed_issues
    assert gh.closed == []


def test_free_text_gets_m2_notice(tmp_path):
    root = make_root(tmp_path)
    gh = FakeGhApi()
    run(root, make_issue(1, body_from(all_normal(), free_text="タンクで守って")), gh=gh)
    assert "自由記述の解釈" in gh.comments[0][1]  # 未対応の明記


# ---- git統合(ローカルbareリポジトリ) ----------------------------------


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _setup_git(tmp_path: Path, root: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    _git("init", "-b", "main", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "init", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("push", "-u", "origin", "main", cwd=root)
    return origin


def test_git_single_commit_contains_save_board_readme(tmp_path):
    root = make_root(tmp_path / "work")
    origin = _setup_git(tmp_path, root)
    run(root, make_issue(1, body_from(all_normal())), do_git=True)
    files = _git("show", "--name-only", "--format=", "HEAD", cwd=root).splitlines()
    assert {"save/state.json", "assets/board.svg", "README.md"} <= set(files)
    tree = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=root).splitlines()
    assert {"save/player.json", "save/party/sora.json", "save/spells/sora_a1.json", "save/log.md"} <= set(tree)
    assert "?v=i1-a0" in (root / "README.md").read_text(encoding="utf-8")
    # pushされている(originの先端=ローカルHEAD)
    assert _git("rev-parse", "HEAD", cwd=root) == _git("rev-parse", "main", cwd=origin)


def test_git_push_conflict_replays_from_remote(tmp_path):
    root = make_root(tmp_path / "work")
    origin = _setup_git(tmp_path, root)
    # 別クローンから先にコミットを積んで、rootのpushを非fast-forwardにする
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
    (other / "notes.txt").write_text("out-of-band change\n", encoding="utf-8")
    _git("add", "notes.txt", cwd=other)
    _git("commit", "-m", "out-of-band", cwd=other)
    _git("push", cwd=other)

    run(root, make_issue(1, body_from(all_normal())), do_git=True)
    # リプレイ後: リモートには割り込みコミットとターンコミットの両方が載っている
    assert _git("rev-parse", "HEAD", cwd=root) == _git("rev-parse", "main", cwd=origin)
    files_in_tree = _git("ls-tree", "-r", "--name-only", "HEAD", cwd=root).splitlines()
    assert "notes.txt" in files_in_tree
    save = load_save(root / "save")
    assert 1 in save.processed_issues
    assert save.battle is not None and save.battle.turn == 2
