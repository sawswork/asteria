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
    constraint_violations,
    normal_attack_effects,
)
import re

from .models import Ability, Battle, Buff, Dot, Enemy, FieldTag, Member, Save, Ultimate
from .rng import Rng
from .spells import budget_for, spell_cost

RECENT_LOG_LIMIT = 10

_GEN_RE = re.compile(r"_gen(\d+)$")  # 生成技のID世代(無印=初代gen0)


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
        m.field_tags = []
        for a in m.abilities:
            a.ready_in = 0
            a.battle_uses = 0
        m.ultimate.battle_uses = 0
    new.stats["chapters"] = new.stats.get("chapters", 0) + 1  # 年代記の章番号(戦闘=1章)
    new.battle = Battle(
        active=True,
        name=str(battle_name or "遭遇戦"),
        turn=1,
        enemies=enemies,
        recent_log=[intro] if intro else [],
    )
    return new


def nemesis_enemy(save: Save) -> Optional[tuple[Enemy, str, str]]:
    """宿敵が居ればフルHPで再構築して返す。(敵, 戦闘名, 登場ログ)。無ければ None。

    進化・歪み(弱点)・進化技は保存されたまま引き継ぐ(戦いの記憶)。戦闘中の一時状態は初期化。
    """
    data = (save.nemesis or {}).get("enemy")
    if not data:
        return None
    e = Enemy.from_dict(dict(data))
    e.hp = e.max_hp
    e.buffs = []
    e.shield = 0
    e.stunned_turns = 0
    e.dots = []
    e.cc_resist = {}
    e.field_tags = []
    e.last_special_turn = 0
    e.evolution_pending = None
    # 進化履歴・歪みは記憶として残すが、HP契機の判定は再戦ごとに開き直す
    # (進化の総数は evolutions_used とティア上限が抑える)
    e.hp_evolution_triggered = False
    intro = f"倒れたはずの{e.name}が、深い唸りとともに再び立ち塞がる——宿敵との再戦。"
    return e, f"宿敵・{e.name}との再戦", intro


@dataclass
class _Ctx:
    save: Save
    battle: Battle
    balance: dict[str, Any]
    rng: Rng
    report: TurnReport
    ability_term: str = "アビリティ"  # 表示用語。world.json の power_system.ability_term で上書き
    enemy_overrides: dict[str, enemy_ai.EnemyDecision] = field(default_factory=dict)  # 知能層の判断
    world: dict[str, Any] = field(default_factory=dict)  # チェイン反応表・歪み弱点プールの参照
    evolution_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)  # AI進化案
    resonance_ids: set[str] = field(default_factory=set)  # このターン共鳴する技ID
    resonance_partner: dict[str, str] = field(default_factory=dict)  # 技ID→相方のメンバーID
    resonance_fired: set[str] = field(default_factory=set)  # 実際に発動した共鳴技ID
    resonance_witness: set[str] = field(default_factory=set)  # 共鳴の片割れ(増幅は受けない)
    resonance_amplified: str = ""  # 増幅を受ける技ID(初代技)
    resonance_mult: float = 1.0
    current_amp: float = 1.0  # 実行中アクションの増幅(共鳴)


def _term(ctx: _Ctx, key: str, default: str = "") -> str:
    """世界固有の語はworld.jsonのsystem_termsから引く(エンジンに固有名詞を書かない不変則)。"""
    return str((ctx.world.get("system_terms") or {}).get(key, default))


def _log(ctx: _Ctx, line: str) -> None:
    ctx.report.lines.append(line)
    ctx.battle.recent_log.append(line)
    del ctx.battle.recent_log[:-RECENT_LOG_LIMIT]


def _damage_amount(ctx: _Ctx, atk: float, power: float, df: float) -> int:
    b = ctx.balance["damage"]
    variance = ctx.rng.uniform(float(b["variance_min"]), float(b["variance_max"]))
    raw = (atk * power - df * float(b["def_coeff"])) * variance
    return max(int(b["min_damage"]), round(raw))


def _field_and_chain_mult(
    ctx: _Ctx, target: Union[Member, Enemy], incoming_field: str | None
) -> float:
    """damage効果に添えられた残留タグ(incoming)のチェイン反応と弱点を解決し、倍率を返す。

    反応が成立: 必要タグを消費して倍率、incomingは残さない。不成立: incomingを対象に付与。
    """
    mult = 1.0
    if incoming_field:
        for reaction in ctx.world.get("chain_reactions", []):
            if str(reaction.get("incoming")) != incoming_field:
                continue
            required = str(reaction.get("requires"))
            hit = next((t for t in target.field_tags if t.name == required), None)
            if hit is None:
                continue
            mult *= float(reaction.get("mult", 1.0))
            if reaction.get("consume", True):
                target.field_tags = [t for t in target.field_tags if t is not hit]
            _log(ctx, str(reaction.get("log", f"【{reaction.get('name', '連鎖')}】が弾けた!")))
            break
        else:
            carry = int(ctx.balance.get("field", {}).get("carry_turns", 2))
            _attach_field(ctx, target, incoming_field, turns=carry, quiet=True)
        # 歪み弱点: 該当タグを添えた攻撃は倍率増(敵のみ)
        if isinstance(target, Enemy):
            for w in target.weaknesses:
                if str(w.get("field")) == incoming_field:
                    mult *= float(w.get("mult", 1.0))
                    _log(ctx, f"{target.name}の歪みを突いた! 弱点【{incoming_field}】!")
    return mult


def _attach_field(
    ctx: _Ctx, target: Union[Member, Enemy], name: str, turns: int, quiet: bool = False
) -> None:
    cap = int(ctx.balance.get("field", {}).get("max_stacks_per_target", 4))
    existing = next((t for t in target.field_tags if t.name == name), None)
    if existing is not None:
        existing.turns_left = max(existing.turns_left, turns)
    elif len(target.field_tags) < cap:
        target.field_tags.append(FieldTag(name=name, turns_left=turns))
    else:
        if not quiet:
            _log(ctx, f"{target.name}への【{name}】は弾かれた(残留タグが飽和)。")
        return
    if not quiet:
        _log(ctx, f"{target.name}に【{name}】が残留した({turns}ターン)。")


def _detect_resonance(ctx: _Ctx, commands: dict[str, Command]) -> None:
    """歴史の共鳴: 初代の技(gen0)と最新世代の生成技(genN)を同一ターンに使うと増幅。

    1戦闘1回。増幅率は「初代技のコストに対する現在予算の余裕」から機械的に決まる
    (= 古い技ほど現在の水準まで引き上げられる。上限は balance.resonance.amp_cap)。
    """
    if ctx.battle.resonance_used:
        return
    used: list[tuple[int, Union[Ability, Ultimate], Member]] = []
    ult_max = int(ctx.balance["ult_gauge"]["max"])
    for m in ctx.save.party:
        if not m.alive:
            continue
        cmd = commands.get(m.role)
        if cmd is None:
            continue
        spell: Union[Ability, Ultimate, None] = None
        if cmd.action in ABILITY_INDEX:
            spell = m.abilities[ABILITY_INDEX[cmd.action]]
        elif cmd.action == ACTION_ULT and m.ult_gauge >= ult_max:
            spell = m.ultimate
        if spell is None:
            continue
        match = _GEN_RE.search(spell.id)
        used.append((int(match.group(1)) if match else 0, spell, m))
    oldest = next((u for u in used if u[0] == 0), None)
    newest = max((u for u in used if u[0] > 0), key=lambda u: u[0], default=None)
    if oldest is None or newest is None:
        return
    _gen, gen0_spell, gen0_member = oldest
    is_ult = isinstance(gen0_spell, Ultimate)
    cost = spell_cost(getattr(gen0_spell, "ct", 0), gen0_spell.effects, ctx.balance, is_ult)
    budget = budget_for(ctx.save.level, gen0_member.role, ctx.balance, is_ult)
    cap = float(ctx.balance.get("resonance", {}).get("amp_cap", 3.0))
    ctx.resonance_mult = max(1.0, min(cap, budget / cost)) if cost > 0 else 1.0
    # 増幅を受けるのは初代技のみ。最新世代の技は既に現行予算いっぱいで作られているため、
    # 同じ倍率を乗せると「誓約で拡張した予算の上に無料の×3」が乗ってしまう
    ctx.resonance_ids = {gen0_spell.id}
    ctx.resonance_amplified = gen0_spell.id
    # 相方が実際に技を放てることが条件(下記 _resonance_amp が発動時に確認する)
    gen0_cmd = commands[gen0_member.role]
    new_cmd = commands[newest[2].role]
    ctx.resonance_partner = {
        gen0_spell.id: (newest[1].id, newest[2].id, new_cmd.target),
        newest[1].id: (gen0_spell.id, gen0_member.id, gen0_cmd.target),
    }
    ctx.resonance_witness = {newest[1].id}  # 最新技は「共鳴の片割れ」だが増幅は受けない


def _resonance_amp(ctx: _Ctx, spell_id: str) -> float:
    """発動時の共鳴増幅率。相方が既に放った/これから確かに放てる場合にのみ効く。

    宣言だけで成立させると、相方が先に倒される・行動不能になる・誓約で不発になっても
    増幅だけが乗り、1戦闘1回の権利も消費されてしまう。
    """
    if spell_id not in ctx.resonance_ids and spell_id not in ctx.resonance_witness:
        return 1.0
    partner_spell_id, partner_member_id, partner_target = ctx.resonance_partner.get(
        spell_id, ("", "", TARGET_AUTO)
    )
    if partner_spell_id not in ctx.resonance_fired:  # 相方はまだ動いていない: 放てるかを見る
        partner = ctx.save.member_by_id(partner_member_id)
        if partner is None or not partner.alive or partner.stunned_turns > 0:
            return 1.0
        partner_spell = _find_spell(partner, partner_spell_id)
        if partner_spell is None:
            return 1.0
        if constraint_violations(partner, partner_spell, ctx.battle, ctx.balance, partner_target):
            return 1.0  # 相方は誓約の条件を満たさず不発になる
    ctx.resonance_fired.add(spell_id)
    _trigger_resonance(ctx)
    return ctx.resonance_mult if spell_id == ctx.resonance_amplified else 1.0


def _find_spell(member: Member, spell_id: str) -> Union[Ability, Ultimate, None]:
    if member.ultimate.id == spell_id:
        return member.ultimate
    return next((a for a in member.abilities if a.id == spell_id), None)


def _trigger_resonance(ctx: _Ctx) -> None:
    if not ctx.battle.resonance_used:
        ctx.battle.resonance_used = True
        _log(ctx, f"✨ 歴史の共鳴! 最初の技と最新の技が時を越えて響き合う(増幅×{ctx.resonance_mult:.1f})")


def _check_evolution_triggers(ctx: _Ctx) -> None:
    """ターン終了時の進化予告。次のターン開始時に _resolve_pending_evolutions が実体化する。"""
    ev = ctx.balance.get("evolution", {})
    max_by_tier = ev.get("max_by_tier", {})
    for e in ctx.battle.enemies:
        if not e.alive or e.evolution_pending is not None:
            continue
        if e.evolutions_used >= int(max_by_tier.get(e.tier, 0)):
            continue
        reason = None
        if not e.hp_evolution_triggered and e.hp <= e.max_hp * float(ev.get("hp_trigger_ratio", 0.5)):
            e.hp_evolution_triggered = True  # HP契機は1戦闘1回だけ(回復で再発火させない)
            reason = "hp"
        elif e.cc_resist.get("stun", 0) >= int(ev.get("cc_trigger_count", 2)) and not any(
            x.get("reason") == "cc" for x in e.evolutions
        ):
            reason = "cc"
        if reason:
            e.evolution_pending = {"reason": reason}
            _log(ctx, f"⚠ {e.name}の身体が軋み、力が渦を巻いている……(進化の前兆)")


def _check_pr_attack(ctx: _Ctx) -> None:
    """ボスの禁忌詠唱(PR攻撃)の状態遷移。実PRの作成・マージ・クローズはrunnerのI/O境界が行う。

    trigger: boss層がHP60%割れで一度だけ "pending"(runnerが実PRを開いて "casting" へ)。
    casting: 詠唱ボスへの合計ダメージが閾値に達すれば "broken"、期限ターンに達すれば "deadline"。
    """
    pa = ctx.balance.get("pr_attack", {})
    battle = ctx.battle
    pr = battle.pr_attack
    if pr is None:
        ratio = float(pa.get("hp_trigger_ratio", 0.6))
        for e in battle.enemies:
            if e.tier == "boss" and e.alive and e.hp <= e.max_hp * ratio:
                battle.pr_attack = {"status": "pending", "enemy_id": e.id}
                _log(
                    ctx,
                    f"⚠ {e.name}は{_term(ctx, 'world_order')}を歪める禁忌の詠唱を始めた"
                    "——PRを封じるか、打ち破るしかない!",
                )
                break
        return
    if pr.get("status") == "casting":
        dealt = int(pr.get("damage_since", 0))
        need = int(pa.get("break_damage", 90))
        deadline = int(pr.get("deadline_turn", battle.turn))
        if dealt >= need:
            pr["status"] = "broken"
            _log(ctx, f"💥 詠唱中に合計{dealt}ダメージ! 禁忌の詠唱を打ち破った!")
        elif battle.turn >= deadline:
            pr["status"] = "deadline"
            _log(ctx, f"🕳 詠唱が完成へ向かう——PRが閉じられていなければ、{_term(ctx, 'world_order')}が歪む……")
        else:
            pr["break_need"] = need - dealt  # ボード表示用(残り必要ダメージ)
            _log(
                ctx,
                f"🕳 禁忌の詠唱は続く(打破まであと{need - dealt}ダメージ/猶予{deadline - battle.turn}ターン)",
            )


def _resolve_pending_evolutions(ctx: _Ctx) -> None:
    """予告済みの進化をターン開始時に実体化する。

    演出(名前・セリフ・進化技の見た目)は ctx.evolution_overrides(生成層で検証済み)から、
    数値(攻撃ボーナス・歪み弱点)は balance とセーブ済み乱数からスクリプトが決める。
    """
    ev = ctx.balance.get("evolution", {})
    for e in ctx.battle.enemies:
        if not e.alive or e.evolution_pending is None:
            continue
        spec = ctx.evolution_overrides.get(e.id) or {}
        evo_name = str(spec.get("name") or _term(ctx, "evolution_fallback_name", "覚醒"))[:14]
        action = spec.get("action")
        if not (
            isinstance(action, dict)
            and action.get("name")
            and isinstance(action.get("effects"), list)
            and action["effects"]
        ):
            power = float(ev.get("fallback_action_power", 1.8))
            action = {
                "name": _term(ctx, "evolution_fallback_action", "渾身の一撃"),
                "effects": [{"tag": "damage", "power": power, "target": "enemy"}],
            }
        e.atk = max(1, round(e.atk * float(ev.get("bonus_mult", 1.3))))
        e.actions["evolved"] = {"name": str(action["name"])[:14], "effects": list(action["effects"])}
        weak_note = ""
        pool = [
            w
            for w in ctx.world.get("distortion_weaknesses", [])
            if all(str(x.get("field")) != w for x in e.weaknesses)
        ]
        if pool:
            picked = str(ctx.rng.choice(pool))
            e.weaknesses.append({"field": picked, "mult": float(ev.get("weakness_mult", 1.5))})
            weak_note = " だがその力は歪みを生んだ(スキャンで見抜ける)。"
        e.evolutions.append(
            {
                "name": evo_name,
                "reason": str(e.evolution_pending.get("reason", "")),
                "turn": ctx.battle.turn,
                "desc": str(spec.get("desc", ""))[:60],
            }
        )
        e.evolutions_used += 1
        e.evolution_pending = None
        _log(
            ctx,
            f"💥 {e.name}は進化した——《{evo_name}》! 新たな技「{e.actions['evolved']['name']}」を得た。{weak_note}",
        )
        if spec.get("line"):
            _log(ctx, f"{e.name}「{str(spec['line'])[:60]}」")


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
    chain_mult = _field_and_chain_mult(ctx, target, effect.get("field"))
    amp = ctx.current_amp
    hits = int(effect.get("hits", 1))
    total = 0
    absorbed_total = 0
    for _ in range(hits):
        if not target.alive:
            break
        dmg = _damage_amount(ctx, actor.eff_atk(), float(effect["power"]), target.eff_def())
        dmg = max(1, round(dmg * chain_mult * amp))
        absorbed, _hp_dmg = _absorb_and_damage(target, dmg)
        absorbed_total += absorbed
        total += dmg
    suffix = f"{hits}連撃で" if hits > 1 else ""
    shield_note = f"(シールドが{absorbed_total}吸収)" if absorbed_total else ""
    _log(ctx, f"{actor.name}の{source_name}! {target.name}に{suffix}{total}ダメージ!{shield_note}")
    actor.hate += total * float(ctx.balance["hate"]["damage_mult"])
    pr = ctx.battle.pr_attack
    if pr and pr.get("status") == "casting" and target.id == pr.get("enemy_id"):
        pr["damage_since"] = int(pr.get("damage_since", 0)) + total  # PR攻撃のブレイク判定用(詠唱ボスへの直接ダメージのみ)
    if not target.alive:
        _log(ctx, f"{target.name}を撃破!")
        if source is not None:
            source.kills += 1
    _check_end(ctx)


def _apply_heal(ctx: _Ctx, actor: Member, effect: dict[str, Any], cmd_target: str, source_name: str) -> None:
    b = ctx.balance["heal"]
    variance = ctx.rng.uniform(float(b["variance_min"]), float(b["variance_max"]))
    amount = max(
        int(b.get("min_heal", 1)),
        round(actor.eff_atk() * float(effect["power"]) * variance * ctx.current_amp),
    )
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
    if target.field_tags:
        _log(ctx, "　残留タグ: " + " ".join(f"【{t.name}】" for t in target.field_tags))
    if target.weaknesses:  # 歪みの開示(進化の代償を見抜く)
        weak_txt = " ".join(f"【{w['field']}】×{w.get('mult', 1.5)}" for w in target.weaknesses)
        _log(ctx, f"　歪み(弱点): {weak_txt} — このタグを添えた攻撃が深く刺さる")
    if target.evolution_pending:
        _log(ctx, "　⚠ 進化の兆候: 力が渦を巻いている(次のターン、何かが起きる)")
    elif target.evolutions:
        _log(ctx, f"　進化履歴: {len(target.evolutions)}回({'/'.join(e.get('name', '?') for e in target.evolutions)})")


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
        elif tag == "field":
            target = _pick_enemy_target(ctx, cmd_target)
            if target is not None:
                _attach_field(ctx, target, str(effect["name"]), int(effect["turns"]))
                actor.hate += float(ctx.balance["hate"]["buff_flat"])
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


def _constraints_hold(ctx: _Ctx, member: Member, spell: Union[Ability, Ultimate], cmd_target: str) -> bool:
    """発動の瞬間に誓約の条件がまだ成立しているか(実行時ガード)。

    validate_commands はターン開始時点で検証するが、解決中に状態は動く
    (味方の回復でHP条件を外れる/対象が倒れて自動再選択で格下の敵に向く等)。
    予算を拡張した代償が空手形にならないよう、発動時にもう一度確かめる。
    """
    if not spell.constraints:
        return True
    violations = constraint_violations(member, spell, ctx.battle, ctx.balance, cmd_target)
    if not violations:
        return True
    _log(ctx, f"{member.name}の「{spell.name}」は誓約の条件を満たさず不発!({violations[0]})")
    return False


def _apply_constraint_backlash(ctx: _Ctx, member: Member, spell: Union[Ability, Ultimate]) -> None:
    """誓約の代償(self_stun_after)。次の1ターンを失う。

    ターン終了時に stunned_turns が1減るため、「使用後Nターン行動不能」は N+1 を積む。
    """
    if "self_stun_after" not in spell.constraints:
        return
    entry = ctx.balance.get("constraints", {}).get("self_stun_after", {})
    turns = int(entry.get("stun_turns", 1)) if isinstance(entry, dict) else 1
    member.stunned_turns = max(member.stunned_turns, turns + 1)
    _log(ctx, f"{member.name}は誓約の反動で身体の自由を失った!({turns}ターン行動不能)")


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
        if not _constraints_hold(ctx, member, ability, cmd.target):
            ability.ready_in = ability.ct  # 不発でもCTは消費(条件待ちで無限に構えられない)
            return
        ability.ready_in = ability.ct
        ability.usage_count += 1
        ability.battle_uses += 1
        ctx.current_amp = _resonance_amp(ctx, ability.id)
        _apply_member_effects(
            ctx, member, ability.effects, cmd.target, f"{ctx.ability_term}「{ability.name}」", ability
        )
        ctx.current_amp = 1.0
        _gain_gauge(ctx, member, int(g["ability"]))
        _apply_constraint_backlash(ctx, member, ability)
        return
    if cmd.action == ACTION_ULT:
        ult_max = int(g["max"])
        if member.ult_gauge < ult_max:
            _log(ctx, f"{member.name}の奥義はゲージ不足で不発!")
            return
        if not _constraints_hold(ctx, member, member.ultimate, cmd.target):
            return  # 奥義は不発ならゲージを温存する(CTと違い蓄積が代償のため)
        member.ult_gauge = 0
        member.ultimate.usage_count += 1
        member.ultimate.battle_uses += 1
        ctx.current_amp = _resonance_amp(ctx, member.ultimate.id)
        _apply_member_effects(
            ctx, member, member.ultimate.effects, cmd.target, f"奥義《{member.ultimate.name}》", member.ultimate
        )
        ctx.current_amp = 1.0
        _apply_constraint_backlash(ctx, member, member.ultimate)
        return


def _apply_enemy_effects(ctx: _Ctx, enemy: Enemy, action: dict[str, Any], target: Member) -> None:
    """敵の行動効果をメンバーに適用する(damage / dot / debuff / stun に対応)。"""
    name = str(action.get("name", "攻撃"))
    for effect in action.get("effects", []):
        if ctx.battle.result:
            return
        tag = effect.get("tag")
        if tag == "damage":
            chain_mult = _field_and_chain_mult(ctx, target, effect.get("field"))
            hits = int(effect.get("hits", 1))
            total = 0
            absorbed_total = 0
            for _ in range(hits):
                if not target.alive:
                    break
                dmg = _damage_amount(ctx, enemy.eff_atk(), float(effect["power"]), target.eff_def())
                dmg = max(1, round(dmg * chain_mult))
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
        elif tag == "field":  # 標的に残留タグを付与(チェイン反応の素材)
            turns = max(1, min(3, int(effect.get("turns", 2))))
            default_tag = _term(ctx, "residue_default", "残滓")
            _attach_field(ctx, target, str(effect.get("name") or default_tag)[:8], turns)
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
    for unit in [*ctx.save.party, *ctx.battle.enemies]:
        for t in unit.field_tags:
            t.turns_left -= 1
        unit.field_tags = [t for t in unit.field_tags if t.turns_left > 0]
    _check_evolution_triggers(ctx)
    _check_pr_attack(ctx)
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
    evolution_overrides: dict[str, dict[str, Any]] | None = None,
) -> tuple[Save, TurnReport]:
    """1ターンを解決する。save は変更せず、新しい Save を返す。

    前提: save.battle が active であり、commands は validate_commands を通過している。
    world は表示用語(power_system)とデータ表(chain_reactions / distortion_weaknesses)の
    参照にのみ使う。倍率などの数値は world のデータ表と balance からスクリプトが決める。
    enemy_overrides は知能層AIの判断(enemy_id→EnemyDecision)。正当性は enemy_ai が検証する。
    evolution_overrides は進化演出のAI案(enemy_id→dict)。generation.generate_evolution で検証済みを渡す。
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
        world=dict(world or {}),
        evolution_overrides=dict(evolution_overrides or {}),
    )

    _log(ctx, f"—— ターン{battle.turn} ——")
    _resolve_pending_evolutions(ctx)
    _detect_resonance(ctx, commands)

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
            nem_id = str(((new.nemesis or {}).get("enemy") or {}).get("id", ""))
            if nem_id and any(e.id == nem_id for e in battle.enemies):
                nem_name = str(new.nemesis["enemy"].get("name", "宿敵"))  # type: ignore[index]
                new.nemesis = None
                new.journal.append(f"宿敵「{nem_name}」を討ち果たした——因縁に決着がついた")
                _log(ctx, f"🗡 宿敵「{nem_name}」との因縁に、ついに決着がついた。")
            _apply_victory_progression(ctx)
        else:
            new.stats["defeats"] = new.stats.get("defeats", 0) + 1
            new.journal.append(f"「{battle.name}」で敗北(ターン{turn_no})")
            foe = next((e for e in battle.enemies if e.alive), None)
            if foe is not None:
                # 宿敵化: 進化・歪み・戦いの記憶ごと保存し、次の戦いで必ず再登場する
                new.nemesis = {"enemy": foe.to_dict(), "battle_name": battle.name}
                new.journal.append(f"{foe.name}は一党を退けた——奴は宿敵として旅路に立ち塞がる")
                _log(ctx, f"……{foe.name}は勝利の咆哮を上げた。奴はもう、ただの敵ではない。")
        limit = int(balance.get("journal_max_entries", 200))
        del new.journal[:-limit]

    report.result = battle.result
    new.rng_counter = rng.counter
    return new, report
