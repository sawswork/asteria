"""敵AI(二層構造)。

ルール層: 挑発ロック遵守+ヘイト最大狙い。行動は「Nターンごとに強撃/special」の固定パターン。
知能層: AIが戦況JSONから返したコマンド(EnemyDecision)を override として受け取り、
このモジュールが正当性を検証する。AIはヘイトを「割引」して判断できるが、
挑発ロックだけは絶対に破れない──タンクの仕事は「賢い敵の自由を奪うこと」。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Battle, Enemy, Member
from .rng import Rng


@dataclass
class EnemyDecision:
    action_key: str  # 敵の actions のキー("normal" / "strong" / 生成敵の "special" 等)
    target_id: str
    line: str = ""  # 知能層AIのセリフ(ルール層は空)
    lock_forced: bool = False  # 挑発ロックにより対象を強制された


def _taunt_holder(battle: Battle, alive: list[Member]) -> Optional[Member]:
    if battle.taunt_turns_left > 0 and battle.taunt_holder_id:
        for m in alive:
            if m.id == battle.taunt_holder_id:
                return m
    return None


def _rule_target(alive: list[Member], rng: Rng) -> Member:
    max_hate = max(m.hate for m in alive)
    top = [m for m in alive if m.hate >= max_hate]
    return top[0] if len(top) == 1 else rng.choice(top)


def _pattern_action_key(enemy: Enemy, battle: Battle, strong_attack_every: int) -> str:
    special_key = "special" if "special" in enemy.actions else ("strong" if "strong" in enemy.actions else None)
    if special_key and strong_attack_every > 0 and battle.turn % strong_attack_every == 0:
        return special_key
    return "normal"


def decide(
    enemy: Enemy,
    battle: Battle,
    party: list[Member],
    rng: Rng,
    strong_attack_every: int,
    override: Optional[EnemyDecision] = None,
) -> Optional[EnemyDecision]:
    alive = [m for m in party if m.alive]
    if not alive:
        return None
    holder = _taunt_holder(battle, alive)

    if override is not None:
        # 知能層: AIの選択を検証してから採用する
        action_key = override.action_key if override.action_key in enemy.actions else "normal"
        target: Optional[Member] = None
        for m in alive:
            if m.id == override.target_id:
                target = m
                break
        lock_forced = False
        if holder is not None and (target is None or target.id != holder.id):
            target = holder  # 挑発ロックは知能層でも絶対(割引ではなく強制)
            lock_forced = True
        if target is None:
            target = _rule_target(alive, rng)
        return EnemyDecision(action_key=action_key, target_id=target.id, line=override.line, lock_forced=lock_forced)

    # ルール層
    target = holder if holder is not None else _rule_target(alive, rng)
    return EnemyDecision(
        action_key=_pattern_action_key(enemy, battle, strong_attack_every),
        target_id=target.id,
    )
