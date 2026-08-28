"""AIプロンプト構築(純粋関数)。

世界観・用語は world.json から、旅の文脈は save.journal(log.md)から取り込む。
AIには常に「JSONのみを出力」させ、数値の裁量は予算内の配分に限定する。
"""
from __future__ import annotations

import json
from typing import Any

from .commands import ROLE_LABELS
from .models import Battle, Enemy, Member, Save
from .spells import budget_for, constraint_multiplier, known_constraints

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
        '- {"tag":"hate","amount":-60〜60,"target":"self"} / {"tag":"taunt","target":"self"}\n'
        '- {"tag":"field","name":"<残留タグ名>","turns":1-3,"target":"enemy"}(盤面に残留タグを置く)\n'
        '- damage効果には "field":"<残留タグ名>" を添えられる(対象に対応タグが残っていればチェイン反応で威力増)'
    )


def _field_menu(world: dict[str, Any]) -> str:
    """world.json のチェイン反応表をAIに提示する(名前と倍率はworldデータの引用)。"""
    tags = world.get("field_tags", {})
    if not tags:
        return ""
    lines = ["この世界の残留タグ: " + " / ".join(f"【{k}】{v}" for k, v in tags.items())]
    seen: set[frozenset[str]] = set()
    parts: list[str] = []
    for r in world.get("chain_reactions", []):
        key = frozenset({str(r.get("requires")), str(r.get("incoming"))})
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{r.get('requires')}+{r.get('incoming')}→{r.get('name')}×{r.get('mult')}")
    if parts:
        lines.append("チェイン反応(順不同で成立・素材タグは消費): " + " / ".join(parts))
    return "\n".join(lines)


def build_spell_generation_prompt(
    save: Save,
    world: dict[str, Any],
    balance: dict[str, Any],
    member: Member,
    slot_label: str,
    incantation: str,
    is_ult: bool,
    constraints: list[str] | None = None,
) -> str:
    constraints = constraints or []
    budget = budget_for(save.level, member.role, balance, is_ult) * constraint_multiplier(
        constraints, balance
    )
    kind = "奥義(CT=0固定・ゲージ制)" if is_ult else "アビリティ(CT1〜5)"
    oath_line = ""
    if constraints:
        table = known_constraints(balance)
        labels = "、".join(str(table.get(c, {}).get("label", c)) for c in constraints)
        oath_line = f"\n誓約(この代償で予算が拡張されている。descや名前に誓約の気配を漂わせてよい): {labels}"
    return f"""あなたはRPGの技デザイナー。以下の依頼から{kind}を1つデザインし、JSONで返す。

{_world_header(world)}
{_journal_tail(save)}

依頼者: {member.name}({ROLE_LABELS.get(member.role, member.role)}・{member.title})
差し替えるスロット: {slot_label}
プレイヤーの詠唱文(この意図を最大限反映すること): {incantation}{oath_line}

制約:
- コスト予算 {budget:.0f} 以内(コスト計算はエンジンが行い、超過は却下される。効果は控えめに)
- {_effect_menu()}
- {_field_menu(world) or "残留タグは自由な名前でよい(8文字以内)"}
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
- {_field_menu(world) or "残留タグは自由な名前でよい(8文字以内)"}
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


def build_evolution_prompt(save: Save, world: dict[str, Any], enemy: Enemy, reason: str) -> str:
    reason_txt = {
        "hp": "HPが半分を割り、追い詰められた",
        "cc": "行動不能を重ねられ、怒りが臨界に達した",
    }.get(reason, "戦いの中で力が臨界に達した")
    field_names = list(world.get("field_tags", {}).keys())
    field_hint = (
        f'- damage効果には残留タグ {json.dumps(field_names, ensure_ascii=False)} を "field" として添えてもよい'
        if field_names
        else "- 残留タグは使わなくてよい"
    )
    return f"""あなたはRPGの敵デザイナー。戦闘中の敵が遂げる「適応進化」の演出と進化技をJSONで返す(能力値ボーナスと弱点はエンジンが決める。あなたは名前と技の見た目のみ)。

{_world_header(world)}

進化する敵: {enemy.name}({enemy.title})/性格: {enemy.personality or "不明"}/ランク: {enemy.tier}
きっかけ: {reason_txt}
現在の技: {json.dumps({k: v.get("name", "?") for k, v in enemy.actions.items()}, ensure_ascii=False)}

制約:
- name は進化の名前(14文字以内)。desc は60文字以内。line は進化の瞬間の咆哮・セリフ(60文字以内・任意)
- action は進化で得る技1つ {{"name","effects"}}: effectsは damage/dot/debuff/stun/buff で1〜2個、控えめに(予算超過は却下)
{field_hint}

返すJSON: {{"name","desc","line","action":{{"name","effects":[...]}}}}
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


def build_book_chapter_prompt(world: dict[str, Any], chapter_no: int, source: str) -> str:
    """1章分の記録を物語として綴らせる。記録の改変は禁じる。"""
    return f"""あなたは年代記を書物へ編む記録者。以下は実際に起きた出来事の記録です。これを一章の物語として綴り、JSONで返す。

{_world_header(world)}

第{chapter_no}章の記録:
---
{source}
---

守ること:
- **記録を改変しない**。勝敗・数値・誰が何をしたかは記録のとおりに。起きていない出来事を足さない
- ログの羅列ではなく地の文で語る。戦いの流れ・転機・人物の息づかいが伝わるように
- 技名・敵名・地名は記録に現れたものだけを使う
- title は章の題(30文字以内)。text は本文(2400文字以内・日本語)

返すJSON: {{"title": "...", "text": "..."}}
{_JSON_ONLY}"""


def build_book_frame_prompt(world: dict[str, Any], save: Save, chapter_titles: list[str]) -> str:
    """書物の表題・序文・終章。"""
    titles = "\n".join(f"- 第{i + 1}章 {t}" for i, t in enumerate(chapter_titles))
    stats = save.stats
    return f"""あなたは年代記を書物へ編む記録者。以下の旅路に、書物の表題・序文・終章を与え、JSONで返す。

{_world_header(world)}
{_journal_tail(save, 12)}

到達点: Lv{save.level} / 勝利{stats.get("victories", 0)}・敗北{stats.get("defeats", 0)} / 仲間{len(save.party) + len(save.roster_extra)}人

章立て:
{titles}

守ること:
- 記録にない出来事を足さない。序文は旅の始まりへの導入、終章は今この時点からの結び
- title は書物の題(30文字以内)。preface / epilogue はそれぞれ900文字以内

返すJSON: {{"title": "...", "preface": "...", "epilogue": "..."}}
{_JSON_ONLY}"""
