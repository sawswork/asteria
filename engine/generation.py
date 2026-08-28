"""生成系オーケストレーション。

AI応答(ai_client経由・スキーマ検証済み)をさらに予算・整合性検証し、
失敗時は決定的なルール層フォールバックへ落とす(ゲームを止めない)。
数値の最終決定権は常にこちら側にある。
"""
from __future__ import annotations

from typing import Any

from . import ai_schemas, prompts
from .ai_client import AiClient, AiError
from .models import Ability, Enemy, Member, Save, Ultimate
from .rng import Rng
from .spells import budget_for, constraint_multiplier, effect_cost, spell_cost, validate_spell

# ---- 技生成 --------------------------------------------------------------


def _next_spell_id(save: Save, member: Member) -> str:
    n = save.stats.get("spells_generated", 0) + 1
    save.stats["spells_generated"] = n
    return f"{member.id}_gen{n}"


def _scale_damage_spell(budget: float, ct: int, balance: dict[str, Any]) -> float:
    """予算いっぱいのdamage powerを求める(0.1刻み・上限4.0)。"""
    from .spells import ct_factor

    power = budget / (float(balance["effect_costs"]["damage_per_power"]) * ct_factor(ct, balance))
    return max(0.3, min(4.0, int(power * 10) / 10))


def fallback_spell(
    save: Save,
    balance: dict[str, Any],
    member: Member,
    incantation: str,
    is_ult: bool,
    budget_mult: float = 1.0,
) -> dict[str, Any]:
    """決定的なフォールバック技(役割テンプレ・予算内)。名前は詠唱文から採る。"""
    budget = budget_for(save.level, member.role, balance, is_ult) * max(1.0, budget_mult)
    if incantation.strip():
        first_line = incantation.strip().splitlines()[0]
        for sep in ("、", "。", ",", "."):  # 読点で切って自然な短い名前にする
            first_line = first_line.split(sep)[0]
        name = first_line[:12] or "無銘の技"
    else:
        name = "無銘の技"
    ct = 0 if is_ult else 2
    c = balance["effect_costs"]
    effects: list[dict[str, Any]]
    if member.role == "healer":
        power = max(0.5, min(4.0, int(budget / float(c["heal_per_power"]) * 10) / 10))
        effects = [{"tag": "heal", "power": power, "target": "ally"}]
        desc = f"味方1人を回復({power}倍)"
    elif member.role == "support":
        mult = 1.0 + budget / (float(c["buff_stat_weight"]["atk"]) * 2 * float(c["buff_party_mult"]))
        mult = max(1.05, min(1.6, int(mult * 100) / 100))
        effects = [{"tag": "buff", "stat": "atk", "mult": mult, "turns": 2, "target": "party"}]
        desc = f"2ターンの間、全員の攻撃{mult}倍"
    elif member.role == "tank":
        power = _scale_damage_spell(budget - float(c["hate_per_point"]) * 20, ct, balance)
        effects = [
            {"tag": "damage", "power": power, "target": "enemy"},
            {"tag": "hate", "amount": 20, "target": "self"},
        ]
        desc = f"攻撃({power}倍)+ヘイト増加"
    else:  # attacker
        power = _scale_damage_spell(budget, ct, balance)
        effects = [{"tag": "damage", "power": power, "target": "enemy"}]
        desc = f"攻撃({power}倍)"
    return {"name": name, "desc": desc, "ct": ct, "effects": effects}


SPELL_GEN_ATTEMPTS = 3  # AI生成の試行回数(却下時は理由を伝えて再生成)


def generate_spell(
    save: Save,
    world: dict[str, Any],
    balance: dict[str, Any],
    ai: AiClient,
    member: Member,
    slot_label: str,
    incantation: str,
    is_ult: bool,
    constraints: list[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """(技dict, AI採用か) を返す。却下時は理由付きで再生成し、尽きたらフォールバック。

    constraints(誓約)があれば予算が乗算拡張された状態で検証する。
    """
    constraints = constraints or []
    base_prompt = prompts.build_spell_generation_prompt(
        save, world, balance, member, slot_label, incantation, is_ult, constraints
    )
    feedback = ""
    try:
        for attempt in range(SPELL_GEN_ATTEMPTS):
            spell = ai.call(
                "spell_gen", base_prompt + feedback, ai_schemas.SPELL_GEN_SCHEMA, purpose="generation"
            )
            errors = validate_spell(spell, balance, save.level, member.role, is_ult, constraints)
            if not errors:
                return spell, True
            print(f"generation: spell rejected ({len(errors)} errors); regenerating")
            feedback = (
                f"\n\n【再生成依頼】前回の案「{spell.get('name', '?')}」は検証で却下された: "
                + " / ".join(errors[:2])
                + "。数値をより控えめにして、制約を厳守した案を出し直すこと。"
            )
    except AiError as e:
        print(f"generation: spell ai failed ({e}); falling back")
    except Exception as e:  # 検証中の想定外もフォールバックへ
        print(f"generation: spell validation error ({type(e).__name__}); falling back")
    mult = constraint_multiplier(constraints, balance)
    return fallback_spell(save, balance, member, incantation, is_ult, mult), False


def install_spell(
    save: Save,
    member: Member,
    slot_label: str,
    spell: dict[str, Any],
    constraints: list[str] | None = None,
) -> str:
    """検証済みの技をスロットへ装着し、新しい技IDを返す(旧技はファイルとして魔導書に残る)。"""
    spell_id = _next_spell_id(save, member)
    constraints = list(constraints or [])
    if slot_label == "奥義":
        member.ultimate = Ultimate(
            id=spell_id, name=str(spell["name"]), effects=list(spell["effects"]), desc=str(spell["desc"]),
            constraints=constraints,
        )
    else:
        index = {"アビ1": 0, "アビ2": 1, "アビ3": 2}[slot_label]
        member.abilities[index] = Ability(
            id=spell_id,
            name=str(spell["name"]),
            ct=int(spell["ct"]),
            effects=list(spell["effects"]),
            desc=str(spell["desc"]),
            constraints=constraints,
        )
    return spell_id


# ---- 技アップデート ------------------------------------------------------


def update_budget(save: Save, balance: dict[str, Any], member: Member, obj: Ability | Ultimate, is_ult: bool) -> float:
    ub = balance.get("update_bonus", {"per_use": 0.6, "per_kill": 3.0, "max_bonus": 30.0})
    bonus = min(
        float(ub["max_bonus"]),
        obj.usage_count * float(ub["per_use"]) + obj.kills * float(ub["per_kill"]),
    )
    # 誓約付きの技は拡張予算のまま進化させる(誓約も引き継がれる)
    base = budget_for(save.level, member.role, balance, is_ult) * constraint_multiplier(obj.constraints, balance)
    return base + bonus


def _shrink_effects_step(effects: list[dict[str, Any]]) -> bool:
    """全効果の可変ノブを一段縮める。縮められるものが無ければ False。"""
    changed = False
    for e in effects:
        if "power" in e and float(e["power"]) > 0.3:
            e["power"] = max(0.3, round(float(e["power"]) * 0.95, 2))
            changed = True
        if "mult" in e:
            m = float(e["mult"])
            if e.get("tag") == "debuff":
                if m < 0.95:
                    e["mult"] = min(0.95, round(1 - (1 - m) * 0.95, 2))
                    changed = True
            elif m > 1.05:
                e["mult"] = max(1.05, round(1 + (m - 1) * 0.95, 2))
                changed = True
        if e.get("tag") == "hate" and abs(float(e["amount"])) > 5:
            e["amount"] = round(float(e["amount"]) * 0.9)
            changed = True
    return changed


def fallback_update_options(
    current: dict[str, Any], budget: float, balance: dict[str, Any], is_ult: bool
) -> list[dict[str, Any]]:
    """決定的な進化3案: 威力寄せ/回転率/堅実強化。全案とも予算内を保証する。"""
    import copy

    def scaled(mult: float, ct_delta: int, direction: str) -> dict[str, Any]:
        spell = copy.deepcopy(current)
        spell["ct"] = 0 if is_ult else max(1, min(5, int(spell["ct"]) + ct_delta))
        for e in spell["effects"]:
            if "power" in e:
                e["power"] = max(0.3, min(4.0, round(float(e["power"]) * mult, 2)))
            if "mult" in e and e.get("tag") == "buff":
                e["mult"] = max(1.05, min(1.6, round(1 + (float(e["mult"]) - 1) * mult, 2)))
        # 予算に収まるまで全ノブを縮める。縮め切れなければ現行の技(必ず予算内)に戻す
        for _ in range(60):
            if spell_cost(spell["ct"], spell["effects"], balance, is_ult) <= budget:
                break
            if not _shrink_effects_step(spell["effects"]):
                spell = copy.deepcopy(current)
                break
        if spell_cost(spell["ct"], spell["effects"], balance, is_ult) > budget:
            spell = copy.deepcopy(current)  # 使い込みボーナスで予算は現行コスト以上なので必ず収まる
        spell["name"] = str(current["name"])[:12] + "・改"
        return {"direction": direction, "spell": spell}

    return [
        scaled(1.2, 1, "威力を高める(CTは伸びる)"),
        scaled(0.95, -1, "回転率を上げる(CT短縮)"),
        scaled(1.08, 0, "堅実に底上げする"),
    ]


def update_spell_options(
    save: Save,
    world: dict[str, Any],
    balance: dict[str, Any],
    ai: AiClient,
    member: Member,
    slot_label: str,
    direction_hint: str,
) -> tuple[list[dict[str, Any]], float, bool]:
    """進化3案と新予算を返す。各案は予算検証済み(不合格案はフォールバック案で置換)。"""
    is_ult = slot_label == "奥義"
    obj: Ability | Ultimate = (
        member.ultimate if is_ult else member.abilities[{"アビ1": 0, "アビ2": 1, "アビ3": 2}[slot_label]]
    )
    current = {"name": obj.name, "desc": obj.desc, "ct": getattr(obj, "ct", 0), "effects": obj.effects}
    budget = update_budget(save, balance, member, obj, is_ult)
    fallbacks = fallback_update_options(current, budget, balance, is_ult)
    used_ai = False
    options = fallbacks
    try:
        prompt = prompts.build_spell_update_prompt(
            save, world, balance, member, slot_label, current, budget, direction_hint, is_ult
        )
        resp = ai.call("spell_update", prompt, ai_schemas.SPELL_UPDATE_SCHEMA, purpose="generation")
        candidates = list(resp["options"])
        merged: list[dict[str, Any]] = []
        for i, cand in enumerate(candidates[:3]):
            spell = cand["spell"]
            budget_errors = validate_spell(spell, balance, save.level, member.role, is_ult)
            cost_ok = spell_cost(int(spell["ct"]), list(spell["effects"]), balance, is_ult) <= budget + 1e-9
            structural_only = [e for e in budget_errors if not e.startswith("予算超過")]
            if not structural_only and cost_ok:
                merged.append(cand)
                used_ai = True
            else:
                merged.append(fallbacks[i])
        options = merged
    except AiError as e:
        print(f"generation: update ai failed ({e}); falling back")
    except Exception as e:  # 検証中の想定外もフォールバックへ
        print(f"generation: update validation error ({type(e).__name__}); falling back")
        options = fallbacks
        used_ai = False
    return options, budget, used_ai


def apply_update_option(save: Save, member: Member, slot_label: str, option: dict[str, Any]) -> str:
    """選択された進化案を適用する(使い込み統計は引き継ぐ)。"""
    is_ult = slot_label == "奥義"
    spell = option["spell"]
    spell_id = _next_spell_id(save, member)
    if is_ult:
        old = member.ultimate
        member.ultimate = Ultimate(
            id=spell_id, name=str(spell["name"]), effects=list(spell["effects"]), desc=str(spell["desc"]),
            usage_count=old.usage_count, kills=old.kills, constraints=list(old.constraints),
        )
    else:
        index = {"アビ1": 0, "アビ2": 1, "アビ3": 2}[slot_label]
        old = member.abilities[index]
        member.abilities[index] = Ability(
            id=spell_id, name=str(spell["name"]), ct=int(spell["ct"]), effects=list(spell["effects"]),
            desc=str(spell["desc"]), usage_count=old.usage_count, kills=old.kills,
            constraints=list(old.constraints),
        )
    return spell_id


# ---- 敵生成 --------------------------------------------------------------


def enemy_stat_guide(level: int, tier: str, balance: dict[str, Any]) -> dict[str, int]:
    es = balance["enemy_scale"]
    tier_mult = float(es["tier_mult"].get(tier, 1.0))
    lv = max(1, level) - 1
    return {
        "hp": round((float(es["hp_base"]) + float(es["hp_per_level"]) * lv) * tier_mult),
        "atk": round((float(es["atk_base"]) + float(es["atk_per_level"]) * lv) * tier_mult),
        "def": round((float(es["def_base"]) + float(es["def_per_level"]) * lv) * tier_mult),
        "agi": round(float(es["agi_base"]) + float(es["agi_per_level"]) * lv),
    }


def _stats_within_tolerance(stats: dict[str, Any], guide: dict[str, int], tolerance: float) -> bool:
    for key, base in guide.items():
        value = float(stats[key])
        if base > 0 and abs(value - base) > base * tolerance:
            return False
    return True


def _special_within_budget(actions: dict[str, Any], save: Save, balance: dict[str, Any]) -> bool:
    # 効果コストは0未満にクランプして合算する(弱いバフ等で強効果のコストを相殺させない)
    def action_cost(action: dict[str, Any]) -> float:
        return sum(max(0.0, effect_cost(e, balance)) for e in action["effects"])

    normal_limit = budget_for(save.level, "attacker", balance, False)
    special_limit = normal_limit * float(balance["enemy_scale"]["special_budget_mult"])
    return action_cost(actions["special"]) <= special_limit and action_cost(actions["normal"]) <= normal_limit


def fallback_enemy(save: Save, world: dict[str, Any], balance: dict[str, Any], rng: Rng, tier: str) -> tuple[Enemy, str]:
    template = rng.choice(world["fallback_enemies"])
    guide = enemy_stat_guide(save.level, tier, balance)
    xp = int(balance["leveling"]["xp_per_tier"].get(tier, 100))
    n = save.stats.get("enemies_generated", 0) + 1
    save.stats["enemies_generated"] = n
    enemy = Enemy(
        id=f"enemy_gen{n}",
        name=str(template["name"]),
        title=str(template.get("title", "")),
        max_hp=guide["hp"],
        hp=guide["hp"],
        atk=guide["atk"],
        df=guide["def"],
        agi=guide["agi"],
        actions={
            "normal": {"name": template["normal_name"], "effects": [{"tag": "damage", "power": 1.0, "target": "enemy"}]},
            "special": {"name": template["special_name"], "effects": list(template["special_effects"])},
        },
        personality=str(template.get("personality", "")),
        tier=tier,
        intelligent=tier != "minion",
        xp=xp,
    )
    return enemy, str(template.get("intro", ""))


def next_battle_tier(save: Save, balance: dict[str, Any]) -> str:
    battles = save.stats.get("victories", 0) + 1
    boss_every = int(balance.get("boss_every_battles", 8))
    elite_every = int(balance.get("elite_every_battles", 4))
    if boss_every > 0 and battles % boss_every == 0:
        return "boss"
    return "elite" if elite_every > 0 and battles % elite_every == 0 else "standard"


def generate_enemy(
    save: Save, world: dict[str, Any], balance: dict[str, Any], ai: AiClient, rng: Rng
) -> tuple[Enemy, str, bool]:
    """次の戦闘の敵を生成する。(敵, 登場ログ, AI採用か)。"""
    tier = next_battle_tier(save, balance)
    guide = enemy_stat_guide(save.level, tier, balance)
    try:
        resp = ai.call(
            "enemy_gen",
            prompts.build_enemy_generation_prompt(save, world, balance, tier, guide),
            ai_schemas.ENEMY_GEN_SCHEMA,
            purpose="generation",
        )
        tolerance = float(balance["enemy_scale"]["stat_tolerance"])
        if _stats_within_tolerance(resp["stats"], guide, tolerance) and _special_within_budget(
            resp["actions"], save, balance
        ):
            n = save.stats.get("enemies_generated", 0) + 1
            save.stats["enemies_generated"] = n
            enemy = Enemy(
                id=f"enemy_gen{n}",
                name=str(resp["name"]),
                title=str(resp.get("title", "")),
                max_hp=int(resp["stats"]["hp"]),
                hp=int(resp["stats"]["hp"]),
                atk=int(resp["stats"]["atk"]),
                df=int(resp["stats"]["def"]),
                agi=int(resp["stats"]["agi"]),
                actions=dict(resp["actions"]),
                personality=str(resp["personality"]),
                tier=tier,
                intelligent=bool(resp["intelligent"]),
                xp=int(balance["leveling"]["xp_per_tier"].get(tier, 100)),
            )
            return enemy, str(resp["intro"]), True
        print("generation: enemy rejected (stats/budget out of bounds); falling back")
    except AiError as e:
        print(f"generation: enemy ai failed ({e}); falling back")
    except Exception as e:  # 検証中の想定外(欠落フィールド等)もフォールバックへ(ゲームを止めない)
        print(f"generation: enemy validation error ({type(e).__name__}); falling back")
    enemy, intro = fallback_enemy(save, world, balance, rng, tier)
    return enemy, intro, False


# ---- 敵の適応進化 --------------------------------------------------------


def fallback_evolution() -> dict[str, Any]:
    """決定的な進化演出(AI不通時)。数値ボーナスはbattle側がbalanceから科すので演出のみ。"""
    return {
        "name": "本能の覚醒",
        "desc": "追い詰められた本能が、力を臨界まで暴走させた",
        "line": "",
        "action": {"name": "覚醒の一撃", "effects": [{"tag": "damage", "power": 1.8, "target": "enemy"}]},
    }


def generate_evolution(
    save: Save, world: dict[str, Any], balance: dict[str, Any], ai: AiClient, enemy: Enemy
) -> tuple[dict[str, Any], bool]:
    """進化の演出(名前・セリフ)と進化技をAIに委ね、予算検証して返す。(演出dict, AI採用か)。

    攻撃ボーナス・歪み弱点の数値は battle._resolve_pending_evolutions がbalanceから決める。
    """
    reason = str((enemy.evolution_pending or {}).get("reason", ""))
    try:
        resp = ai.call(
            "evolution",
            prompts.build_evolution_prompt(save, world, enemy, reason),
            ai_schemas.EVOLUTION_SCHEMA,
            purpose="generation",
        )
        limit = (
            budget_for(save.level, "attacker", balance, False)
            * float(balance["enemy_scale"]["special_budget_mult"])
            * float(balance.get("evolution", {}).get("action_budget_mult", 1.3))
        )
        cost = sum(max(0.0, effect_cost(e, balance)) for e in resp["action"]["effects"])
        if cost <= limit:
            return resp, True
        print("generation: evolution rejected (action over budget); falling back")
    except AiError as e:
        print(f"generation: evolution ai failed ({e}); falling back")
    except Exception as e:  # 検証中の想定外もフォールバックへ(ゲームを止めない)
        print(f"generation: evolution validation error ({type(e).__name__}); falling back")
    return fallback_evolution(), False


# ---- 勧誘 ----------------------------------------------------------------


def _role_base_stats(world: dict[str, Any], balance: dict[str, Any], role: str, level: int) -> dict[str, int]:
    base = next(m for m in world["initial_party"] if m["role"] == role)
    growth = balance["leveling"]["growth"].get(role, {})
    lv = max(1, level) - 1
    return {
        "max_hp": int(base["max_hp"]) + int(growth.get("max_hp", 0)) * lv,
        "atk": int(base["atk"]) + int(growth.get("atk", 0)) * lv,
        "def": int(base["def"]) + int(growth.get("def", 0)) * lv,
        "agi": int(base["agi"]) + int(growth.get("agi", 0)) * lv,
    }


def generate_recruit(
    save: Save, world: dict[str, Any], balance: dict[str, Any], ai: AiClient, rng: Rng
) -> tuple[Member, bool]:
    """勧誘イベント: 新メンバーを生成しロスターに追加できる形で返す。(メンバー, AI採用か)。"""
    role = rng.choice(list(world.get("recruit_pool_roles", ["attacker", "support", "tank", "healer"])))
    n = save.stats.get("recruits", 0) + 1
    save.stats["recruits"] = n
    member_id = f"recruit{n}"
    stats = _role_base_stats(world, balance, role, save.level)

    name, title, spells_src, used_ai = "流れ星の旅人", "名もなき同行者", None, False
    try:
        resp = ai.call(
            "recruit", prompts.build_recruit_prompt(save, world, role), ai_schemas.RECRUIT_SCHEMA,
            purpose="generation",
        )
        ability_ok = all(
            not validate_spell(sp, balance, save.level, role, False) for sp in resp["abilities"]
        )
        ult_ok = not validate_spell(resp["ultimate"], balance, save.level, role, True)
        name = str(resp["name"])
        title = str(resp.get("title", ""))
        if ability_ok and ult_ok:
            spells_src = (list(resp["abilities"]), resp["ultimate"])
            used_ai = True
        else:
            print("generation: recruit spells rejected; using fallback spells")
        save.journal.append(f"{name}が仲間に加わった({resp.get('background', '')[:40]})")
    except AiError as e:
        print(f"generation: recruit ai failed ({e}); falling back")
        save.journal.append(f"{name}が仲間に加わった")

    if spells_src is None:
        abilities_dicts = [fallback_spell(save, balance, _tmp_member(member_id, role, name, title, stats), "", False) for _ in range(3)]
        ultimate_dict = fallback_spell(save, balance, _tmp_member(member_id, role, name, title, stats), "", True)
    else:
        abilities_dicts, ultimate_dict = spells_src

    member = Member(
        id=member_id,
        role=role,
        name=name,
        title=title,
        max_hp=stats["max_hp"],
        hp=stats["max_hp"],
        atk=stats["atk"],
        df=stats["def"],
        agi=stats["agi"],
        abilities=[
            Ability(
                id=f"{member_id}_a{i + 1}",
                name=str(sp["name"]),
                ct=max(1, int(sp["ct"])),
                effects=list(sp["effects"]),
                desc=str(sp["desc"]),
            )
            for i, sp in enumerate(abilities_dicts)
        ],
        ultimate=Ultimate(
            id=f"{member_id}_ult",
            name=str(ultimate_dict["name"]),
            effects=list(ultimate_dict["effects"]),
            desc=str(ultimate_dict["desc"]),
        ),
        hate=float(balance["hate"]["initial"]),
    )
    return member, used_ai


def _tmp_member(member_id: str, role: str, name: str, title: str, stats: dict[str, int]) -> Member:
    from .models import Ability as _A, Ultimate as _U

    return Member(
        id=member_id, role=role, name=name, title=title,
        max_hp=stats["max_hp"], hp=stats["max_hp"], atk=stats["atk"], df=stats["def"], agi=stats["agi"],
        abilities=[_A(id="_", name="_", ct=1, effects=[])] * 3, ultimate=_U(id="_", name="_", effects=[]),
    )
