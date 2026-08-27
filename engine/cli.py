"""ローカル実行CLI。Actionsなしで戦闘エンジンを回す。

例:
  python -m engine.cli --input fixtures/turn.json            # 1ターンをドライラン(セーブ不変)
  python -m engine.cli --input fixtures/turn.json --write    # セーブ・ボード・READMEを更新
  python -m engine.cli --reset                               # 初期セーブを再生成
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import battle as battle_mod
from . import board as board_mod
from . import screen
from .commands import Command, validate_commands
from .save_io import DEFAULT_SEED, load_json, load_save, new_save, write_save

DEFAULT_REPO = "OWNER/REPO"  # ローカル表示用のプレースホルダ(実機はGITHUB_REPOSITORYを使う)


def _write_screen(save, world, balance, root: Path, repo: str, cache_key: str) -> None:
    svg = board_mod.build_board_svg(save, world, balance)
    board_file = root / "assets/board.svg"
    board_file.parent.mkdir(parents=True, exist_ok=True)
    board_file.write_text(svg, encoding="utf-8")
    (root / "README.md").write_text(
        screen.render_readme(save, world, repo, cache_key), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="戦闘エンジン ローカルCLI")
    parser.add_argument("--input", help="ターンコマンドJSON(fixtures/turn.json 形式)")
    parser.add_argument("--save", default="save/state.json")
    parser.add_argument("--world", default="world/world.json")
    parser.add_argument("--balance", default="world/balance.json")
    parser.add_argument("--root", default=".")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--write", action="store_true", help="セーブ・ボード・READMEを書き込む")
    parser.add_argument("--reset", action="store_true", help="初期セーブを再生成する")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--mock", action="store_true", help="AI応答をfixturesの固定JSONにする(M1では未使用)")
    args = parser.parse_args(argv)

    root = Path(args.root)
    world = load_json(root / args.world)
    balance = load_json(root / args.balance)

    if args.reset:
        save = new_save(world, balance, seed=args.seed)
        write_save(save, root / args.save)
        _write_screen(save, world, balance, root, args.repo, "local")
        print(f"初期セーブを生成しました: {args.save} (seed={args.seed})")
        return 0

    if not args.input:
        parser.error("--input か --reset のどちらかを指定してください")

    save = load_save(root / args.save)
    if save.battle is None or not save.battle.active:
        save = battle_mod.start_battle(save, world, balance)
        print(f"新しい戦いが始まった: {save.battle.name}")  # type: ignore[union-attr]

    turn_input = load_json(root / args.input)
    commands = {
        role: Command(role=role, action=str(c["action"]), target=str(c.get("target", "自動")))
        for role, c in turn_input["commands"].items()
    }
    assert save.battle is not None
    errors = validate_commands(save, save.battle, commands, balance)
    if errors:
        print("不正な手(ターン不消費):")
        for e in errors:
            print(f"  - {e.role}: {e.reason}")
        return 2

    new, report = battle_mod.resolve_turn(save, commands, balance, world)
    for line in report.lines:
        print(line)
    summary = {
        "turn": report.turn,
        "result": report.result,
        "enemies": [{"name": e.name, "hp": e.hp, "max_hp": e.max_hp} for e in new.battle.enemies]
        if new.battle
        else [],
        "party": [{"name": m.name, "hp": m.hp, "gauge": m.ult_gauge} for m in new.party],
        "rng_counter": new.rng_counter,
    }
    print(json.dumps(summary, ensure_ascii=False))

    if args.write:
        write_save(new, root / args.save)
        _write_screen(new, world, balance, root, args.repo, "local")
        print("セーブ・ボード・READMEを更新しました。")
    else:
        print("(ドライラン: セーブは変更されていません。--write で反映)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
