"""セーブスキーマv2(ファイル分割)とv1移行のテスト。"""
import json

from engine.battle import resolve_turn
from engine.save_io import load_save, write_save
from tests.conftest import all_normal_commands


def test_v2_files_layout(tmp_path, battle_save):
    write_save(battle_save, tmp_path / "save")
    root = tmp_path / "save"
    assert (root / "state.json").exists()
    assert (root / "player.json").exists()
    assert (root / "log.md").exists()
    for mid in ("sora", "ryuno", "gante", "mio"):
        assert (root / "party" / f"{mid}.json").exists()
    assert (root / "spells" / "sora_a1.json").exists()
    assert (root / "spells" / "sora_ult.json").exists()
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert "party" not in state  # パーティはstate.jsonに埋め込まない


def test_v2_roundtrip_preserves_everything(tmp_path, battle_save, balance):
    mid, _ = resolve_turn(battle_save, all_normal_commands(), balance)
    write_save(mid, tmp_path / "save")
    loaded = load_save(tmp_path / "save")
    assert loaded.to_dict() == mid.to_dict()


def test_v1_migration(tmp_path, battle_save):
    # v1形式(単一state.json)で書いたセーブを透過的に読める
    root = tmp_path / "save"
    root.mkdir()
    (root / "state.json").write_text(
        json.dumps(battle_save.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    loaded = load_save(root)
    assert loaded.to_dict() == battle_save.to_dict()
    # 書き戻すとv2レイアウトに移行される
    write_save(loaded, root)
    assert (root / "player.json").exists()
    assert load_save(root).to_dict() == battle_save.to_dict()


def test_replaced_spell_file_is_kept(tmp_path, battle_save):
    """スロットから外れた技のファイルは削除されない(魔導書=成長史)。"""
    write_save(battle_save, tmp_path / "save")
    old_spell = tmp_path / "save/spells/sora_a1.json"
    assert old_spell.exists()
    attacker = battle_save.member_by_role("attacker")
    attacker.abilities[0].id = "sora_new1"
    attacker.abilities[0].name = "新しい技"
    write_save(battle_save, tmp_path / "save")
    assert (tmp_path / "save/spells/sora_new1.json").exists()
    assert old_spell.exists()  # 旧技は魔導書に残る


def test_journal_persisted_as_log_md(tmp_path, battle_save):
    battle_save.journal.append("テストの記録")
    write_save(battle_save, tmp_path / "save")
    text = (tmp_path / "save/log.md").read_text(encoding="utf-8")
    assert "- テストの記録" in text
    assert load_save(tmp_path / "save").journal[-1] == "テストの記録"