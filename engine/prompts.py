"""AIプロンプト構築(純粋関数)。

世界観・用語は world.json から、旅の文脈は save.journal(log.md)から取り込む。
AIには常に「JSONのみを出力」させ、数値の裁量は予算内の配分に限定する。
"""
from __future__ import annotations

import json
from typing import Any

from .commands import ROLE_LABELS
from .models import Battle, Member, Save
from .spells import budget_for

_JSON_ONLY = "出力はJSONオブジェクトのみ。説明文・前置き・コードフェンスは一切不要。"


def _world_header(world: dict[str, Any]) -> str:
    ps = world["power_system"]
    return (
        f"世界「{world['world_name']}」: {world.get('worldview', world.get('tagline', ''))}\n"
        f"力の体系: {ps['name']}(技の呼称は「{ps['ability_term']}」)。命名は{world.get('naming', 'この世界観に合う日本語')}。"
    )


def _journal_tail(save: Save, n: int = 8) -> str:
    tail = save.journal[-n:]
    return "旅の記憶:\n" + "\n".join(f"- {line}" for line in tail) if tail else ""


def _effect_menu() -> str:
    return (
        "使える効果タグ(この辞書の組み合わせのみ。動作は辞書内、名前と演出は自由):\n"
        '- {"tag":"damage","power":0.3-4.0,"hits":1-3,"target":"enemy"}\n'
        '- {"tag":"heal","power":0.5-4.0,"target":"ally"|"party"}\n'
        '- {"tag":"buff","stat":"atk"|"def"|"agi","mult":1.05-1.6,"turns":1-3,"target":"self"|"party"}\n'
        '- {"tag":"debuff","stat":"atk"|"def"|"agi","mult":0.5-0.95,"turns":1-3,"target":"enemy"}\n'
        '- {"tag":"stun","turns":1-2,"target":"enemy"}(高コスト)\n'
        '- {"tag":"dot","power":0.2-1.5,"turns":1-3,"target":"enemy"}\n'
        '- {"tag":"shield","power":0.5-4.0,"target":"self"|"ally"|"party"}\n'
        '- {"tag":"scan","target":"enemy"} / {"tag":"dispel","target":"enemy"}\n'
        '- {"tag":"hate","amount":-60〜60,"target":"self"} / {"tag":"taunt","target":"self"}'
    )


def build_spell_generation_prompt(
    save: Save,
    world: dict[str, Any],
    balance: dict[str, Any],
    member: Member,
    slot_label: str,
    incantation: str,
    is_ult: bool,
) -> str:
    budget = budget_for(save.level, member.role, balance, is_ult)
    kind = "奥義(CT=0固定・ゲージ制)" if is_ult else "アビリティ(CT1〜5)"
    return f"""あなたはRPGの技デザイナー。以下の依頼から{kind}を1つデザインし、JSONで返す。

{_world_header(world)}
{_journal_tail(save)}

依頼者: {member.name}({ROLE_LABELS.get(member.role, member.role)}・{member.title})
差し替えるスロット: {slot_label}
プレイヤーの詠唱文(この意図を最大限反映すること): {incantation}

制約:
- コスト予算 {budget:.0f} 以内(コスト計算はエンジンが行い、超過は却下される。効果は控えめに)
- {_effect_menu()}
- 効果は1〜3個。名前は14文字以内・この世界の言葉で。descは70文字以内で効果を正確に説明

返すJSON: {{"name": "...", "desc": "...", "ct": {0 if is_ult else "1〜5"}, "effects": [...]}}
{_JSON_ONLY}"""


def build_spell_update_prompt(
    save: Save,
    world: dict[str, Any],
    balance: dict[str, Any],
    member: Member,
    slot_label: str,
    current: dict[str, Any],
    budget: float,
    direction_hint: str,
    is_ult: bool,
) -> str:
    return f"""あなたはRPGの技デザイナー。既存の技の「進化方向3案」をJSONで返す。

{_world_header(world)}
{_journal_tail(save)}

対象: {member.name}({ROLE_LABELS.get(member.role, member.role)})の{slot_label}
現在の技: {json.dumps(current, ensure_ascii=False)}
使い込みボーナス込みの新予算: {budget:.0f}(超過案は却下される)
プレイヤーの希望: {direction_hint or "特になし(多様な3方向を提案)"}

制約:
- 3案は方向性が異なること(例: 火力特化/範囲化/効果追加)。名前は元の面影を残して進化させる
- {_effect_menu()}
- 奥義は ct=0 固定。アビリティは ct 1〜5

返すJSON: {{"options": [{{"direction": "方向の短い説明", "spell": {{"name","desc","ct","effects"}}}} ×3]}}
{_JSON_ONLY}"""


def build_enemy_generation_prompt(
    save: Save, world: dict[str, Any], balance: dict[str, Any], tier: str, stat_guide: dict[str, int]
) -> str:
    return f"""あなたはRPGの敵デザイナー。次の戦闘に登場する敵を1体デザインし、JSONで返す。

{_world_header(world)}
{_journal_tail(save)}

パーティレベル: {save.level} / 敵ランク: {tier}
ステータス基準値(±18%以内で調整可): {json.dumps(stat_guide, ensure_ascii=False)}

制約:
- actions.normal は damage 中心の基本攻撃、actions.special は個性の出る技(dot/debuff/stun/buff可)
- personality は 狡猾/凶暴/臆病/冷酷/誇り高い から選ぶ。intelligent は standard 以上なら true 推奨
- intro は登場ログ1行(80文字以内)

返すJSON: {{"name","title","personality","tier":"{tier}","intelligent",true/false,"stats":{{"hp","atk","def","agi"}},"actions":{{"normal":{{"name","effects"}},"special":{{"name","effects"}}}},"intro"}}
{_JSON_ONLY}"""


def build_turn_prompt(save: Save, world: dict[str, Any], battle: Battle) -> str:
    party_state = [
        {
            "role": m.role,
            "name": m.name,
            "hp": f"{m.hp}/{m.max_hp}",
            "hate": int(m.hate),
            "alive": m.alive,
        }
        for m in save.party
    ]
    enemies_state = [
        {
            "enemy_id": e.id,
            "name": e.name,
            "personality": e.personality,
            "hp": f"{e.hp}/{e.max_hp}",
            "actions": list(e.actions.keys()),
            "stunned": e.stunned_turns > 0,
        }
        for e in battle.enemies
        if e.alive and e.intelligent
    ]
    taunt = (
        f"挑発ロック中: 対象は必ずタンク({battle.taunt_holder_id})になる(残り{battle.taunt_turns_left}ターン)。"
        if battle.taunt_turns_left > 0
        else "挑発ロックなし。"
    )
    return f"""あなたはRPGの敵AI兼実況。知能の高い敵の行動をJSONで返す(結果や数値は決めない。行動と対象の選択のみ)。

世界「{world['world_name']}」の戦闘「{battle.name}」ターン{battle.turn}。
味方(敵から見た標的): {json.dumps(party_state, ensure_ascii=False)}
あなたが動かす敵: {json.dumps(enemies_state, ensure_ascii=False)}
{taunt}

判断基準:
- ヘイト最大の対象を狙うのが自然だが、知能の高い敵はヘイトを「割引」して戦術的に判断してよい
  (例: 回復役(healer)を先に潰す、瀕死の標的に止めを刺す)。性格に従うこと
- ただし挑発ロック中はタンク以外を狙えない(狙ってもエンジンに強制される)
- action_key は敵の actions にあるキーのみ。line は短い戦闘セリフ(任意)
- flavor はこのターンの実況を彩る短文0〜2行(任意)

返すJSON: {{"enemy_commands":[{{"enemy_id","action_key","target_role","line"}}],"flavor":["..."]}}
{_JSON_ONLY}"""


def build_recruit_prompt(save: Save, world: dict[str, Any], role: str) -> str:
    existing = "、".join(f"{m.name}({ROLE_LABELS.get(m.role, m.role)})" for m in save.party)
    return f"""あなたはRPGのキャラクターデザイナー。旅の途中で仲間になる人物を1人デザインし、JSONで返す。

{_world_header(world)}
{_journal_tail(save)}

既存の仲間: {existing}
新しい仲間の役割: {role}({ROLE_LABELS.get(role, role)})

制約:
- ステータス数値はエンジンが決める。あなたは名前・人格・背景・戦闘セリフ・技(アビリティ3+奥義1)のみ
- {_effect_menu()}
- 技は役割に合った控えめな威力で(予算検証で却下されたらエンジンのテンプレに置換される)

返すJSON: {{"name","title","role":"{role}","personality","background","battle_cry","abilities":[技×3],"ultimate":技}}
{_JSON_ONLY}"""
