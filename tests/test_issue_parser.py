from pathlib import Path

from engine.issue_parser import parse_issue_body

ROOT = Path(__file__).resolve().parent.parent


def test_parse_fixture_body():
    body = (ROOT / "fixtures/issue_body.md").read_text(encoding="utf-8")
    parsed = parse_issue_body(body)
    assert parsed.errors == []
    assert parsed.free_text == ""
    assert parsed.commands["attacker"].action == "アビ1"
    assert parsed.commands["attacker"].target == "敵1"
    assert parsed.commands["support"].action == "通常攻撃"
    assert parsed.commands["tank"].action == "アビ1"
    assert parsed.commands["healer"].target == "自動"


def test_parse_crlf_body():
    body = (ROOT / "fixtures/issue_body.md").read_text(encoding="utf-8").replace("\n", "\r\n")
    parsed = parse_issue_body(body)
    assert parsed.errors == []
    assert parsed.commands["attacker"].action == "アビ1"


def test_missing_action_is_error():
    body = "### アタッカーの行動\n\n通常攻撃\n"
    parsed = parse_issue_body(body)
    assert any("サポート" in e for e in parsed.errors)


def test_free_text_captured():
    body = (
        "### アタッカーの行動\n\n通常攻撃\n\n### アタッカーの対象\n\n自動\n\n"
        "### サポートの行動\n\n通常攻撃\n\n### サポートの対象\n\n自動\n\n"
        "### タンクの行動\n\n通常攻撃\n\n### タンクの対象\n\n自動\n\n"
        "### ヒーラーの行動\n\n通常攻撃\n\n### ヒーラーの対象\n\n自動\n\n"
        "### 自由記述(任意・M2で対応)\n\nタンクで守って集中攻撃\n"
    )
    parsed = parse_issue_body(body)
    assert parsed.errors == []
    assert parsed.free_text == "タンクで守って集中攻撃"


def test_missing_target_defaults_to_auto():
    body = (
        "### アタッカーの行動\n\n通常攻撃\n\n### アタッカーの対象\n\n_No response_\n\n"
        "### サポートの行動\n\n通常攻撃\n\n### サポートの対象\n\n自動\n\n"
        "### タンクの行動\n\n通常攻撃\n\n### タンクの対象\n\n自動\n\n"
        "### ヒーラーの行動\n\n通常攻撃\n\n### ヒーラーの対象\n\n自動\n"
    )
    parsed = parse_issue_body(body)
    assert parsed.commands["attacker"].target == "自動"
