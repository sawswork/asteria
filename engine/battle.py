"""戦闘解決(純粋関数)。

resolve_turn は Save とコマンド一式を受け取り、新しい Save とターンレポートを返す。
入力は変更しない。乱数は Save に記録された seed/counter から再現される。

M1の仕様:
- 敵味方混合で実効AGI降順に行動解決(同値はセーブ済み乱数でタイブレーク)
- CT: 使用時に ready_in=ct をセットし、毎ターン終了時に1減算(ready_in>0 の間は使用不可)
- 奥義ゲージ: 通常攻撃/アビ/待機/被弾で加算、満タンで奥義解放、使用で0
- ヘイト: 与ダメ・回復量・バフで蓄積。挑発は最大ヘイト×係数+固定値&強制ロック
- バフ: turns_left をターン終了時に減算、0で消滅
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from . import enemy_ai
from .commands import (
    ABILITY_INDEX,
    ACTION_NORMAL,
    ACTION_ULT,
    ACTION_WAIT,
    LABEL_TO_ROLE,
    TARGET_AUTO,
    TARGET_ENEMIES,
    Command,
    normal_attack_effects,
)
from .models import Ability, Battle, Buff, Dot, Enemy, Member, Save, Ultimate
from .rng import Rng

RECENT_LOG_LIMIT = 10


@dataclass
class TurnReport:
    turn: int
    lines: list[str] = field(default_factory=list)
    result: Optional[str] = None  # None | "victory" | "defeat"


def first_battle_enemies(world: dict[str, Any]) -> tuple[list[Enemy], str, str]:
    """world定義の最初の戦闘(固定データ)。(敵リスト, 戦闘名, 登場ログ)。"""
    spec = world["first_battle"]
    enemies: list[Enemy] = []
    for enemy_id in spec["enemy_ids"]:
        e = world["enemies"][enemy_id]
        enemies.append(
            Enemy(
                id=str(e["id"]),
                name=str(e["name"]),
                title=str(e.get("title", "")),
                max_hp=int(e["max_hp"]),
                hp=int(e["max_hp"]),
                atk=int(e["atk"]),
                df=int(e["def"]),
                agi=int(e["agi"]),
                actions=dict(e["actions"]),
            )
        )
    return enemies, str(spec["battle_name"]), str(spec.get("intro", ""))


def start_battle(
    save: Save,
    world: dict[str, Any],
    balance: dict[str, Any],
    enemies: list[Enemy] | None = None,
    battle_name: str | None = None,
    intro: str | None = None,
) -> Save:
    """新しい戦闘を開始したSaveを返す。enemies省略時はworld定義の初期戦闘。

    パーティは拠点帰還扱いで全快する(奥義ゲージのみ持ち越し。DECISIONS.md参照)。
    """
    new = copy.deepcopy(save)
    if enemies is None:
        enemies, battle_name, intro = first_battle_enemies(world)
    else:
        enemies = copy.deepcopy(enemies)
    initial_hate = float(balance["hate"]["initial"])
    for m in new.party:
        m.hp = m.max_hp
        m.hate = initial_hate
        m.buffs = []
        m.shield = 0
        m.stunned_turns = 0
        m.dots = []
        for a in m.abilities:
            a.ready_in = 0
    new.battle = Battle(
        active=True,
        name=str(battle_name or "遭遇戦"),
        turn=1,
        enemies=enemies,
        recent_log=[intro] if intro else [],
    )
    return new


@dataclass
class _Ctx:
    save: Save
    battle: Battle
    balance: dict[str, Any]
    rng: Rng
    report: TurnReport
    ability_term: str = "アビリティ"  # 表示用語。world.json の power_system.ability_term で上書き
    enemy_overrides: dict[str, enemy_ai.EnemyDecision] = field(default_factory=dict)  # 知能層の判断


def _log(ctx: _Ctx, line: str) -> None:
    ctx.report.lines.append(line)
    ctx.battle.recent_log.append(line)
    del ctx.battle.recent_log[:-RECENT_LOG_LIMIT]


def _damage_amount(ctx: _Ctx, atk: float, power: float, df: float) -> int:
    b = ctx.balance["damage"]
    variance = ctx.rng.uniform(float(b["variance_min"]), float(b["variance_max"]))
    raw = (atk * power - df * float(b["def_coeff"])) * variance
    return max(int(b["min_damage"]), round(raw))


def _absorb_and_damage(target: Union[Member, Enemy], dmg: int) -> tuple[int, int]:
    """シールドで吸収してからHPへ。(吸収量, HPダメージ) を返す。"""
    absorbed = min(target.shield, dmg)
    target.shield -= absorbed
    hp_dmg = dmg - absorbed
    target.hp = max(0, target.hp - hp_dmg)
    return absorbed, hp_dmg


def _gain_gauge(ctx: _Ctx, member: Member, amount: int) -> None:
    cap = int(ctx.balance["ult_gauge"]["max"])
    member.ult_gauge = min(cap, member.ult_gauge + amount)


def _check_end(ctx: _Ctx) -> None:
    if ctx.battle.result:
        return
    if all(not e.alive for e in ctx.battle.enemies):
        ctx.battle.result = "victory"
        ctx.battle.active = False
        _log(ctx, f"勝利! 「{ctx.battle.name}」を制した!")
    elif all(not m.alive for m in ctx.save.party):
        ctx.battle.result = "defeat"
        ctx.battle.active = False
        _log(ctx, "全滅……パーティは拠点へ送り返された。")


def _pick_enemy_target(ctx: _Ctx, cmd_target: str) -> Optional[Enemy]:
    if cmd_target in TARGET_ENEMIES:
        idx = TARGET_ENEMIES.index(cmd_target)
        if idx < len(ctx.battle.enemies) and ctx.battle.enemies[idx].alive:
            return ctx.battle.enemies[idx]
    for e in ctx.battle.enemies:  # 自動 / 指定先が倒れていた場合は先頭の生存敵
        if e.alive:
            return e
    return None


def _pick_heal_target(ctx: _Ctx, cmd_target: str) -> Optional[Member]:
    if cmd_target in LABEL_TO_ROLE:
        m = ctx.save.member_by_role(LABEL_TO_ROLE[cmd_target])
        if m is not None and m.alive:
            return m
    alive = [m for m in ctx.save.party if m.alive]
    if not alive:
        return None
    return min(alive, key=lambda m: (m.hp / m.max_hp, m.id))  # 自動=HP割合最小


def _apply_member_damage(
    ctx: _Ctx,
    actor: Member,
    effect: dict[str, Any],
    cmd_target: str,
    source_name: str,
    source: Union[Ability, Ultimate, None] = None,
) -> None:
    target = _pick_enemy_target(ctx, cmd_target)
    if target is None:
        _log(ctx, f"{actor.name}の{source_name}は空を切った(敵がいない)。")
        return
    hits = int(effect.get("hits", 1))
    total = 0
    absorbed_total = 0
    for _ in range(hits):
        if not target.alive:
            break
        dmg = _damage_amount(ctx, actor.eff_atk(), float(effect["power"]), target.eff_def())
        absorbed, _hp_dmg = _absorb_and_damage(target, dmg)
        absorbed_total += absorbed
        total += dmg
    suffix = f"{hits}連撃で" if hits > 1 else ""
    shield_note = f"(シールドが{absorbed_total}吸収)" if absorbed_total else ""
    _log(ctx, f"{actor.name}の{source_name}! {target.name}に{suffix}{total}ダメージ!{shield_note}")
    actor.hate += total * float(ctx.balance["hate"]["damage_mult"])
    if not target.alive:
        _log(ctx, f"{target.name}を撃破!")
        if source is not None:
            source.kills += 1
    _check_end(ctx)


def _apply_heal(ctx: _Ctx, actor: Member, effect: dict[str, Any], cmd_target: str, source_name: str) -> None:
    b = ctx.balance["heal"]
    variance = ctx.rng.uniform(float(b["variance_min"]), float(b["variance_max"]))
    amount = max(int(b.get("min_heal", 1)), round(actor.eff_atk() * float(effect["power"]) * variance))
    targets: list[Member]
    if effect.get("target") == "party":
        targets = [m for m in ctx.save.party if m.alive]
    else:
        t = _pick_heal_target(ctx, cmd_target)
        targets = [t] if t else []
    healed_total = 0
    for t in targets:
        healed = min(amount, t.max_hp - t.hp)
        t.hp += healed
        healed_total += healed
    names = "全員" if len(targets) > 1 else (targets[0].name if targets else "誰も")
    _log(ctx, f"{actor.name}の{source_name}! {names}のHPが{healed_total}回復!")
    actor.hate += healed_total * float(ctx.balance["hate"]["heal_mult"])


def _apply_buff(ctx: _Ctx, actor: Member, effect: dict[str, Any], source_name: str) -> None:
    buff = Buff(stat=str(effect["stat"]), mult=float(effect["mult"]), turns_left=int(effect["turns"]))
    if effect.get("target") == "party":
        targets = [m for m in ctx.save.party if m.alive]
    else:  # self
        targets = [actor]
    for t in targets:
        t.buffs.append(copy.deepcopy(buff))
    stat_label = {"atk": "攻撃", "def": "防御", "agi": "素早さ"}.get(buff.stat, buff.stat)
    who = "全員" if len(targets) > 1 else actor.name
    _log(ctx, f"{actor.name}の{source_name}! {who}の{stat_label}が{buff.mult}倍({buff.turns_left}ターン)!")
    actor.hate += float(ctx.balance["hate"]["buff_flat"])


def _apply_taunt(ctx: _Ctx, actor: Member, source_name: str) -> None:
    hb = ctx.balance["hate"]
    max_hate = max(m.hate for m in ctx.save.party)
    actor.hate = max_hate * float(hb["taunt_mult"]) + float(hb["taunt_flat"])
    lock = int(ctx.balance["taunt"]["lock_turns"])
    ctx.battle.taunt_holder_id = actor.id
    ctx.battle.taunt_turns_left = lock
    _log(ctx, f"{actor.name}の{source_name}! 敵の狙いを{lock}ターンの間、自分に固定した!")


def _apply_member_debuff(ctx: _Ctx, actor: Member, effect: dict[str, Any], cmd_target: str, source_name: str) -> None:
    target = _pick_enemy_target(ctx, cmd_target)
    if target is None:
        return
    buff = Buff(stat=str(effect["stat"]), mult=float(effect["mult"]), turns_left=int(effect["turns"]))
    target.buffs.append(buff)
    stat_label = {"atk": "攻撃", "def": "防御", "agi": "素早さ"}.get(buff.stat, buff.stat)
    _log(ctx, f"{actor.name}の{source_name}! {target.name}の{stat_label}が{buff.mult}倍に低下({buff.turns_left}ターン)!")
    actor.hate += float(ctx.balance["hate"]["buff_flat"])


def _apply_stun(ctx: _Ctx, actor: Member, effect: dict[str, Any], cmd_target: str, source_name: str) -> None:
    target = _pick_enemy_target(ctx, cmd_target)
    if target is None:
        return
    resist = target.cc_resist.get("stun", 0)
    effective = max(0, int(effect["turns"]) - resist)
    effective = min(effective, int(ctx.balance["cc"]["max_stun_turns"]))
    target.cc_resist["stun"] = resist + int(ctx.balance["cc"]["stun_resist_step"])
    if effective <= 0:
        _log(ctx, f"{actor.name}の{source_name}! しかし{target.name}は耐性で振りほどいた!")
        return
    target.stunned_turns = max(target.stunned_turns, effective)
    _log(ctx, f"{actor.name}の{source_name}! {target.name}は{effective}ターン行動不能!(CC耐性が上昇)")


def _apply_dot(ctx: _Ctx, actor: Member, effect: dict[str, Any], cmd_target: str, source_name: str) -> None:
    target = _pick_enemy_target(ctx, cmd_target)
    if target is None:
        return
    damage = max(1, round(actor.eff_atk() * float(effect["power"])))
    turns = int(effect["turns"])
    target.dots.append(Dot(damage=damage, turns_left=turns, source=source_name))
    _log(ctx, f"{actor.name}の{source_name}! {target.name}は継続ダメージ状態({damage}/ターン×{turns})!")
    actor.hate += damage * turns * 0.5 * float(ctx.balance["hate"]["damage_mult"])


def _apply_shield(ctx: _Ctx, actor: Member, effect: dict[str, Any], cmd_target: str, source_name: str) -> None:
    amount = max(1, round(actor.eff_atk() * float(effect["power"])))
    if effect.get("target") == "party":
        targets = [m for m in ctx.save.party if m.alive]
    elif effect.get("target") == "self":
        targets = [actor]
    else:
        t = _pick_heal_target(ctx, cmd_target)
        targets = [t] if t else []
    for t in targets:
        t.shield += amount
    names = "全員" if len(targets) > 1 else (targets[0].name if targets else "誰も")
    _log(ctx, f"{actor.name}の{source_name}! {names}に{amount}のシールド!")
    actor.hate += float(ctx.balance["hate"]["buff_flat"])


def _apply_scan(ctx: _Ctx, actor: Member, cmd_target: str, source_name: str) -> None:
    target = _pick_enemy_target(ctx, cmd_target)
    if target is None:
        return
    if target.id not in ctx.battle.scanned:
        ctx.battle.scanned.append(target.id)
    hate_order = sorted((m for m in ctx.save.party if m.alive), key=lambda m: -m.hate)
    hate_txt = " > ".join(f"{m.name}{int(m.hate)}" for m in hate_order)
    personality = f" 性格={target.personality}" if target.personality else ""
    _log(ctx, f"{actor.name}の{source_name}! {target.name}を分析: 攻{target.atk} 防{target.df} 速{target.agi}{personality}")
    _log(ctx, f"　敵のヘイト: {hate_txt}")


def _apply_dispel(ctx: _Ctx, actor: Member, cmd_target: str, source_name: str) -> None:
    target = _pick_enemy_target(ctx, cmd_target)
    if target is None:
        return
    removed = [b for b in target.buffs if b.mult > 1.0]
    target.buffs = [b for b in target.buffs if b.mult <= 1.0]  # 弱体(デバフ)は残す
    if removed:
        _log(ctx, f"{actor.name}の{source_name}! {target.name}の強化を{len(removed)}つ打ち消した!")
    else:
        _log(ctx, f"{actor.name}の{source_name}! しかし{target.name}に強化はなかった。")


def _apply_member_effects(
    ctx: _Ctx,
    actor: Member,
    effects: list[dict[str, Any]],
    cmd_target: str,
    source_name: str,
    source: Union[Ability, Ultimate, None] = None,
) -> None:
    for effect in effects:
        if ctx.battle.result:
            return
        tag = effect.get("tag")
        if tag == "damage":
            _apply_member_damage(ctx, actor, effect, cmd_target, source_name, source)
        elif tag == "heal":
            _apply_heal(ctx, actor, effect, cmd_target, source_name)
        elif tag == "buff":
            _apply_buff(ctx, actor, effect, source_name)
        elif tag == "debuff":
            _apply_member_debuff(ctx, actor, effect, cmd_target, source_name)
        elif tag == "stun":
            _apply_stun(ctx, actor, effect, cmd_target, source_name)
        elif tag == "dot":
            _apply_dot(ctx, actor, effect, cmd_target, source_name)
        elif tag == "shield":
            _apply_shield(ctx, actor, effect, cmd_target, source_name)
        elif tag == "scan":
            _apply_scan(ctx, actor, cmd_target, source_name)
        elif tag == "dispel":
            _apply_dispel(ctx, actor, cmd_target, source_name)
        elif tag == "taunt":
            _apply_taunt(ctx, actor, source_name)
        elif tag == "hate":
            actor.hate = max(0.0, actor.hate + float(effect["amount"]))
            amount = int(effect["amount"])
            if amount >= 0:
                _log(ctx, f"{actor.name}は敵の注意を引いた(ヘイト+{amount})。")
            else:
                _log(ctx, f"{actor.name}は気配を消した(ヘイト{amount})。")
        # 未知タグは無視(効果タグ辞書の拡張はエンジン更新で行う)


def _member_act(ctx: _Ctx, member: Member, cmd: Command) -> None:
    g = ctx.balance["ult_gauge"]
    if cmd.action == ACTION_WAIT:
        _gain_gauge(ctx, member, int(g["wait"]))
        _log(ctx, f"{member.name}は力を溜めた(ゲージ+{int(g['wait'])})。")
        return
    if cmd.action == ACTION_NORMAL:
        _apply_member_effects(ctx, member, normal_attack_effects(ctx.balance), cmd.target, "攻撃")
        _gain_gauge(ctx, member, int(g["normal_attack"]))
        return
    if cmd.action in ABILITY_INDEX:
        ability = member.abilities[ABILITY_INDEX[cmd.action]]
        if ability.ready_in > 0:  # 実行時ガード(検証済みだが防御的に)
            _log(ctx, f"{member.name}の{ability.name}はまだ使えない!")
            return
        ability.ready_in = ability.ct
        ability.usage_count += 1
        _apply_member_effects(
            ctx, member, ability.effects, cmd.target, f"{ctx.ability_term}「{ability.name}」", ability
        )
        _gain_gauge(ctx, member, int(g["ability"]))
        return
    if cmd.action == ACTION_ULT:
        ult_max = int(g["max"])
        if member.ult_gauge < ult_max:
            _log(ctx, f"{member.name}の奥義はゲージ不足で不発!")
            return
        member.ult_gauge = 0
        member.ultimate.usage_count += 1
        _apply_member_effects(
            ctx, member, member.ultimate.effects, cmd.target, f"奥義《{member.ultimate.name}》", member.ultimate
        )
        return


def _apply_enemy_effects(ctx: _Ctx, enemy: Enemy, action: dict[str, Any], target: Member) -> None:
    """敵の行動効果をメンバーに適用する(damage / dot / debuff / stun に対応)。"""
    name = str(action.get("name", "攻撃"))
    for effect in action.get("effects", []):
        if ctx.battle.result:
            return
        tag = effect.get("tag")
        if tag == "damage":
            hits = int(effect.get("hits", 1))
            total = 0
            absorbed_total = 0
            for _ in range(hits):
                if not target.alive:
                    break
                dmg = _damage_amount(ctx, enemy.eff_atk(), float(effect["power"]), target.eff_def())
                absorbed, _hp = _absorb_and_damage(target, dmg)
                absorbed_total += absorbed
                total += dmg
                _gain_gauge(ctx, target, int(ctx.balance["ult_gauge"]["hit_taken"]))
            shield_note = f"(シールドが{absorbed_total}吸収)" if absorbed_total else ""
            _log(ctx, f"{enemy.name}の{name}! {target.name}に{total}ダメージ!{shield_note}")
            if not target.alive:
                _log(ctx, f"{target.name}は倒れた……")
        elif tag == "dot":
            damage = max(1, round(enemy.eff_atk() * float(effect["power"])))
            target.dots.append(Dot(damage=damage, turns_left=int(effect["turns"]), source=name))
            _log(ctx, f"{enemy.name}の{name}! {target.name}は継続ダメージ状態({damage}/ターン)!")
        elif tag == "debuff":
            buff = Buff(stat=str(effect["stat"]), mult=float(effect["mult"]), turns_left=int(effect["turns"]))
            target.buffs.append(buff)
            stat_label = {"atk": "攻撃", "def": "防御", "agi": "素早さ"}.get(buff.stat, buff.stat)
            _log(ctx, f"{enemy.name}の{name}! {target.name}の{stat_label}が低下!")
        elif tag == "stun":
            target.stunned_turns = max(target.stunned_turns, min(int(effect["turns"]), int(ctx.balance["cc"]["max_stun_turns"])))
            _log(ctx, f"{enemy.name}の{name}! {target.name}は行動不能!")
        elif tag == "buff":  # 自己強化
            enemy.buffs.append(Buff(stat=str(effect["stat"]), mult=float(effect["mult"]), turns_left=int(effect["turns"])))
            _log(ctx, f"{enemy.name}の{name}! {enemy.name}は力を高めた!")
    _check_end(ctx)


def _enemy_act(ctx: _Ctx, enemy: Enemy) -> None:
    decision = enemy_ai.decide(
        enemy,
        ctx.battle,
        ctx.save.party,
        ctx.rng,
        int(ctx.balance["enemy"]["strong_attack_every"]),
        override=ctx.enemy_overrides.get(enemy.id),
    )
    if decision is None:
        return
    target = ctx.save.member_by_id(decision.target_id)
    if target is None or not target.alive:
        return
    action = enemy.actions[decision.action_key]
    if decision.action_key != "normal":
        enemy.last_special_turn = ctx.battle.turn  # 特殊技の使用ターンを記録(連発防止)
    if decision.line:
        _log(ctx, f"{enemy.name}「{decision.line}」")
    if decision.lock_forced:
        _log(ctx, f"{enemy.name}は狙いを変えようとしたが、挑発から逃れられない!")
    _apply_enemy_effects(ctx, enemy, action, target)


def _clear_dead_taunt(ctx: _Ctx) -> None:
    """挑発保持者が倒れたらロックを即時解除する(敵AI・ボード表示と状態を一致させる)。"""
    if not ctx.battle.taunt_holder_id:
        return
    holder = ctx.save.member_by_id(ctx.battle.taunt_holder_id)
    if holder is None or not holder.alive:
        ctx.battle.taunt_holder_id = None
        ctx.battle.taunt_turns_left = 0
        if holder is not None and not ctx.battle.result:
            _log(ctx, f"{holder.name}が倒れ、敵の狙いの固定が解けた!")


def _tick_dots(ctx: _Ctx) -> None:
    """ターン終了時にDoTを発火(スナップショットダメージ・シールド貫通なしで吸収適用)。"""
    for target in [*ctx.save.party, *ctx.battle.enemies]:
        if not target.alive or not target.dots:
            continue
        total = sum(d.damage for d in target.dots)
        _absorb_and_damage(target, total)
        _log(ctx, f"{target.name}は継続ダメージで{total}を受けた!")
        if not target.alive:
            _log(ctx, f"{target.name}は倒れた……")
        for d in target.dots:
            d.turns_left -= 1
        target.dots = [d for d in target.dots if d.turns_left > 0]
    _check_end(ctx)
    _clear_dead_taunt(ctx)


def _end_of_turn(ctx: _Ctx) -> None:
    _tick_dots(ctx)
    if ctx.battle.result:
        return  # DoTで決着した場合、ターン番号や残り効果はそのまま確定する
    for m in ctx.save.party:
        for a in m.abilities:
            if a.ready_in > 0:
                a.ready_in -= 1
        for b in m.buffs:
            b.turns_left -= 1
        m.buffs = [b for b in m.buffs if b.turns_left > 0]
        if m.stunned_turns > 0:
            m.stunned_turns -= 1
    for e in ctx.battle.enemies:
        for b in e.buffs:
            b.turns_left -= 1
        e.buffs = [b for b in e.buffs if b.turns_left > 0]
        if e.stunned_turns > 0:
            e.stunned_turns -= 1
    if ctx.battle.taunt_turns_left > 0:
        ctx.battle.taunt_turns_left -= 1
        if ctx.battle.taunt_turns_left == 0:
            ctx.battle.taunt_holder_id = None
    ctx.battle.turn += 1


def xp_to_next(level: int, balance: dict[str, Any]) -> int:
    lv = balance["leveling"]
    return round(float(lv["xp_curve_base"]) * float(lv["xp_curve_growth"]) ** (max(1, level) - 1))


def _apply_victory_progression(ctx: _Ctx) -> None:
    """勝利時のXP付与とレベルアップ(パーティ共有レベル・生成権+1・役割別成長)。"""
    lv = ctx.balance["leveling"]
    gained = sum(
        e.xp or int(lv["xp_per_tier"].get(e.tier, lv["xp_per_tier"]["standard"]))
        for e in ctx.battle.enemies
    )
    save = ctx.save
    save.xp += gained
    _log(ctx, f"経験値{gained}を獲得!(累計{save.xp})")
    while save.xp >= xp_to_next(save.level, ctx.balance):
        save.xp -= xp_to_next(save.level, ctx.balance)
        save.level += 1
        save.spell_tokens += 1
        for m in [*save.party, *save.roster_extra]:
            growth = lv["growth"].get(m.role, {})
            m.max_hp += int(growth.get("max_hp", 0))
            m.atk += int(growth.get("atk", 0))
            m.df += int(growth.get("def", 0))
            m.agi += int(growth.get("agi", 0))
            if m.hp > 0:  # 戦闘不能者はレベルアップでは蘇生しない
                m.hp = min(m.max_hp, m.hp + int(growth.get("max_hp", 0)))
        _log(ctx, f"⭐ レベルアップ! パーティはLv{save.level}になった! 技生成権+1(所持{save.spell_tokens})")
        save.journal.append(f"パーティがLv{save.level}に到達")


def resolve_turn(
    save: Save,
    commands: dict[str, Command],
    balance: dict[str, Any],
    world: dict[str, Any] | None = None,
    enemy_overrides: dict[str, enemy_ai.EnemyDecision] | None = None,
) -> tuple[Save, TurnReport]:
    """1ターンを解決する。save は変更せず、新しい Save を返す。

    前提: save.battle が active であり、commands は validate_commands を通過している。
    world は表示用語(power_system)の参照にのみ使う。数値には一切影響しない。
    enemy_overrides は知能層AIの判断(enemy_id→EnemyDecision)。正当性は enemy_ai が検証する。
    """
    if save.battle is None or not save.battle.active:
        raise ValueError("battle is not active")
    new = copy.deepcopy(save)
    battle = new.battle
    assert battle is not None
    rng = Rng(new.rng_seed, new.rng_counter)
    report = TurnReport(turn=battle.turn)
    ability_term = str(((world or {}).get("power_system") or {}).get("ability_term") or "アビリティ")
    ctx = _Ctx(
        save=new,
        battle=battle,
        balance=balance,
        rng=rng,
        report=report,
        ability_term=ability_term,
        enemy_overrides=dict(enemy_overrides or {}),
    )

    _log(ctx, f"—— ターン{battle.turn} ——")

    # 行動順: 実効AGI降順、同値はセーブ済み乱数でタイブレーク
    actors: list[Union[Member, Enemy]] = [m for m in new.party if m.alive] + [
        e for e in battle.enemies if e.alive
    ]
    ordered = sorted(
        ((a.eff_agi(), rng.uniform(0.0, 1.0), a) for a in actors),
        key=lambda t: (-t[0], -t[1]),
    )

    for _, _, actor in ordered:
        if battle.result:
            break
        if not actor.alive:
            continue
        if actor.stunned_turns > 0:
            _log(ctx, f"{actor.name}は行動不能で動けない!")
            continue
        if isinstance(actor, Enemy):
            _enemy_act(ctx, actor)
        else:
            cmd = commands.get(actor.role)
            if cmd is None:
                continue
            _member_act(ctx, actor, cmd)
        _clear_dead_taunt(ctx)

    if not battle.result:
        _end_of_turn(ctx)
    if battle.result:
        turn_no = battle.turn
        if battle.result == "victory":
            new.stats["victories"] = new.stats.get("victories", 0) + 1
            new.journal.append(f"「{battle.name}」に勝利(ターン{turn_no})")
            _apply_victory_progression(ctx)
        else:
            new.stats["defeats"] = new.stats.get("defeats", 0) + 1
            new.journal.append(f"「{battle.name}」で敗北(ターン{turn_no})")
        limit = int(balance.get("journal_max_entries", 200))
        del new.journal[:-limit]

    report.result = battle.result
    new.rng_counter = rng.counter
    return new, report
