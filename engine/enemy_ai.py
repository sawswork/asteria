"""敵AI ルール層(M1)。

挑発ロック中はロック保持者を必ず狙い、それ以外はヘイト最大の生存メンバーを狙う。
同値はセーブ済み乱数でタイブレーク。行動は「Nターンごとに強撃」の固定パターン。
知能層(戦況JSON→コマンドJSON)は M2 で追加する。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Battle, Enemy, Member
from .rng import Rng


@dataclass
class EnemyDecision:
    action_key: str  # "normal" | "strong"
    target_id: str


def decide(
    enemy: Enemy,
    battle: Battle,
    party: list[Member],
    rng: Rng,
    strong_attack_every: int,
) -> EnemyDecision | None:
    alive = [m for m in party if m.alive]
    if not alive:
        return None

    target: Member | None = None
    if battle.taunt_turns_left > 0 and battle.taunt_holder_id:
        for m in alive:
            if m.id == battle.taunt_holder_id:
                target = m
                break
    if target is None:
        max_hate = max(m.hate for m in alive)
        top = [m for m in alive if m.hate >= max_hate]
        target = top[0] if len(top) == 1 else rng.choice(top)

    action_key = "strong" if (
        strong_attack_every > 0
        and battle.turn % strong_attack_every == 0
        and "strong" in enemy.actions
    ) else "normal"
    return EnemyDecision(action_key=action_key, target_id=target.id)
