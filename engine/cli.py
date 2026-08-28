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
from . import generation, screen, turn_ai
from .ai_client import AiClient
from .commands import Command, validate_commands
from .rng import Rng
from .save_io import DEFAULT_SEED, load_json, load_save, new_save, write_save

DEFAULT_REPO = "OWNER/REPO"  # ローカル表示用のプレースホルダ(実機はGITHUB_REPOSITORYを使う)


def _write_screen(save, world, balance, root: Path, repo: str, cache_key: str) -> None:
    svg = board_mod.build_board_svg(save, world, balance)
    board_file = root / "assets/board.svg"
    board_file.parent.mkdir(parents=True, exist_ok=True)
    board_file.write_text(svg, encoding="utf-8")
    has_scene = (root / "assets/scene.svg").exists()
    (root / "README.md").write_text(
        screen.render_readme(save, world, repo, cache_key, has_scene=has_scene), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="戦闘エンジン ローカルCLI")
    parser.add_argument("--input", help="ターンコマンドJSON(fixtures/turn.json 形式)")
    parser.add_argument("--save", default="save", help="セーブディレクトリ")
    parser.add_argument("--world", default="world/world.json")
    parser.add_argument("--balance", default="world/balance.json")
    parser.add_argument("--root", default=".")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--write", action="store_true", help="セーブ・ボード・READMEを書き込む")
    parser.add_argument("--reset", action="store_true", help="初期セーブを再生成する")
    parser.add_argument("--process-assets", action="store_true", help="assets/raw/ を処理してシーンを再合成する")
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

    if args.process_assets:
        from . import assets as assets_mod
        from . import scene as scene_mod

        manifest = assets_mod.process_raw_assets(root)
        if manifest is None:
            print("assets/raw/ に素材がありません")
            return 0
        print(f"素材を処理しました(品質{manifest['quality']}・縮尺{manifest['scale']}・合計{manifest['total_b64_bytes']}B)")
        save = load_save(root / args.save)
        if save.battle is not None and save.battle.active:
            svg = scene_mod.build_scene_svg(save, world, str(root))
            (root / "assets/scene.svg").write_text(svg, encoding="utf-8")
            _write_screen(save, world, balance, root, args.repo, "local")
            print("シーンを再合成しました: assets/scene.svg")
        return 0

    if not args.input:
        parser.error("--input か --reset か --process-assets を指定してください")

    ai = AiClient(mock=args.mock, fixtures_dir=root / "fixtures/ai", config_path=root / "config/ai.json")
    save = load_save(root / args.save)
    started_new_battle = False
    if save.battle is None or not save.battle.active:
        started_new_battle = True
        is_first = (save.stats.get("victories", 0) + save.stats.get("defeats", 0)) == 0
        if is_first:
            save = battle_mod.start_battle(save, world, balance)
        else:
            rng = Rng(save.rng_seed, save.rng_counter)
            enemy, intro, _used_ai = generation.generate_enemy(save, world, balance, ai, rng)
            save.rng_counter = rng.counter
            save = battle_mod.start_battle(
                save, world, balance, enemies=[enemy], battle_name=f"{enemy.name}との戦い", intro=intro
            )
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

    overrides, flavor = turn_ai.compute_enemy_overrides(save, world, ai)
    new, report = battle_mod.resolve_turn(save, commands, balance, world, overrides)
    report.lines.extend(f"({line})" for line in flavor)
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
        if started_new_battle:
            from .turn_runner import prepare_scene

            prepare_scene(root, new, world)
        _write_screen(new, world, balance, root, args.repo, "local")
        print("セーブ・ボード・READMEを更新しました。")
    else:
        print("(ドライラン: セーブは変更されていません。--write で反映)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
