"""技予算・スキーマ検証のテスト。M1の固定技16個が予算に収まることがフェアネスの基準。"""
from engine.spells import budget_for, spell_cost, validate_spell


def _initial_spells(world):
    for m in world["initial_party"]:
        for a in m["abilities"]:
            yield m["role"], False, {"name": a["name"], "desc": a["desc"], "ct": a["ct"], "effects": a["effects"]}
        u = m["ultimate"]
        yield m["role"], True, {"name": u["name"], "desc": u["desc"], "ct": 0, "effects": u["effects"]}


def test_all_initial_spells_fit_level1_budget(world, balance):
    failures = []
    for role, is_ult, spell in _initial_spells(world):
        errors = validate_spell(spell, balance, level=1, role=role, is_ult=is_ult)
        if errors:
            failures.append((spell["name"], errors))
    assert failures == []


def test_over_budget_rejected(balance):
    spell = {"name": "壊れ技", "desc": "", "ct": 0, "effects": [{"tag": "damage", "power": 4.0, "hits": 3, "target": "enemy"}]}
    errors = validate_spell(spell, balance, level=1, role="attacker", is_ult=False)
    assert any("予算超過" in e for e in errors)


def test_unknown_tag_rejected(balance):
    spell = {"name": "謎の技", "desc": "", "ct": 1, "effects": [{"tag": "instant_win", "target": "enemy"}]}
    errors = validate_spell(spell, balance, level=1, role="attacker", is_ult=False)
    assert errors  # スキーマ違反


def test_out_of_range_power_rejected(balance):
    spell = {"name": "過剰火力", "desc": "", "ct": 5, "effects": [{"tag": "damage", "power": 99, "target": "enemy"}]}
    assert validate_spell(spell, balance, level=1, role="attacker", is_ult=False)


def test_extra_property_rejected(balance):
    spell = {
        "name": "チート", "desc": "", "ct": 1,
        "effects": [{"tag": "damage", "power": 1.0, "target": "enemy", "ignore_defense": True}],
    }
    assert validate_spell(spell, balance, level=1, role="attacker", is_ult=False)


def test_ult_with_ct_rejected(balance):
    spell = {"name": "変な奥義", "desc": "", "ct": 3, "effects": [{"tag": "damage", "power": 2.0, "target": "enemy"}]}
    errors = validate_spell(spell, balance, level=1, role="attacker", is_ult=True)
    assert any("奥義" in e for e in errors)


def test_budget_grows_with_level(balance):
    assert budget_for(5, "attacker", balance, False) > budget_for(1, "attacker", balance, False)
    assert budget_for(1, "attacker", balance, True) > budget_for(1, "attacker", balance, False)


def test_ct_discount_lowers_cost(balance):
    effects = [{"tag": "damage", "power": 2.0, "target": "enemy"}]
    assert spell_cost(4, effects, balance, False) < spell_cost(0, effects, balance, False)


def test_stun_is_expensive(balance):
    effects = [{"tag": "stun", "turns": 2, "target": "enemy"}]
    cost = spell_cost(0, effects, balance, False)
    assert cost > budget_for(1, "attacker", balance, False)  # Lv1ではCT最大でも単独スタン2Tは買えない水準