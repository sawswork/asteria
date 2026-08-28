"""ターン処理のAI呼び出し(1ターン1回に同梱: 敵AI判断+ログ味付け+演出指示)。

知能層の敵がいる場合のみ呼び、失敗時は空(=ルール層)を返す。ゲームは止めない。
"""
from __future__ import annotations

from typing import Any

from . import ai_schemas, prompts
from .ai_client import AiClient, AiError
from .enemy_ai import EnemyDecision
from .models import Save


def compute_enemy_overrides(
    save: Save, world: dict[str, Any], ai: AiClient
) -> tuple[dict[str, EnemyDecision], list[str]]:
    """(enemy_id→EnemyDecision, flavor行) を返す。知能層不要/AI失敗時は空。"""
    battle = save.battle
    if battle is None or not battle.active:
        return {}, []
    if not any(e.alive and e.intelligent for e in battle.enemies):
        return {}, []
    try:
        resp = ai.call(
            "turn",
            prompts.build_turn_prompt(save, world, battle),
            ai_schemas.ENEMY_TURN_SCHEMA,
            purpose="turn",
        )
    except AiError as e:
        print(f"turn_ai: fallback to rule layer ({e})")
        return {}, []
    intelligent_ids = {e.id for e in battle.enemies if e.alive and e.intelligent}
    overrides: dict[str, EnemyDecision] = {}
    for cmd in resp.get("enemy_commands", []):
        enemy_id = str(cmd["enemy_id"])
        if enemy_id not in intelligent_ids:
            continue  # 知能層でない/存在しない敵のIDは無視(ルール層の敵は乗っ取れない)
        member = save.member_by_role(str(cmd["target_role"]))
        if member is None:
            continue
        overrides[enemy_id] = EnemyDecision(
            action_key=str(cmd["action_key"]),
            target_id=member.id,
            line=str(cmd.get("line", ""))[:60],
        )
    flavor = [str(x)[:70] for x in resp.get("flavor", [])][:2]
    return overrides, flavor
