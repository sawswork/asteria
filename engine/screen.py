"""README(ゲーム画面)の生成。

READMEは毎ターン全体を再生成する。ボード画像は相対URL+キャッシュ回避クエリ(?v=<cache_key>)で参照する:
相対パスは非公開リポジトリでも認証付きで表示され、クエリはcamoのキャッシュキーを変えるため毎ターン更新が反映される。
世界の固有名詞は world.json から受け取る(エンジン不変則)。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .models import Save

TEMPLATE_FILE = "turn.yml"


def _prefill_link(repo_slug: str, fields: dict[str, str]) -> str:
    params = "&".join(f"{k}={quote(v)}" for k, v in fields.items())
    return f"https://github.com/{repo_slug}/issues/new?template={TEMPLATE_FILE}&{params}"


def render_readme(
    save: Save, world: dict[str, Any], repo_slug: str, cache_key: str, has_scene: bool = False
) -> str:
    world_name = str(world["world_name"])
    tagline = str(world.get("tagline", ""))
    gauge_term = str(world["power_system"]["ult_gauge_term"])
    if cache_key == "local":
        board_url = "assets/board.svg"  # ローカル生成・初期コミット用
        scene_url = "assets/scene.svg"
    else:
        board_url = f"assets/board.svg?v={cache_key}"
        scene_url = f"assets/scene.svg?v={cache_key}"
    scene_block = ""
    if has_scene and save.battle and save.battle.active:
        scene_block = f"![戦闘シーン]({scene_url})\n\n"
    new_turn_url = f"https://github.com/{repo_slug}/issues/new?template={TEMPLATE_FILE}"
    all_normal_url = _prefill_link(
        repo_slug,
        {
            "attacker_action": "通常攻撃",
            "attacker_target": "自動",
            "support_action": "通常攻撃",
            "support_target": "自動",
            "tank_action": "通常攻撃",
            "tank_target": "自動",
            "healer_action": "通常攻撃",
            "healer_target": "自動",
        },
    )

    generate_url = f"https://github.com/{repo_slug}/issues/new?template=generate.yml"
    update_url = f"https://github.com/{repo_slug}/issues/new?template=update.yml"
    rewind_url = f"https://github.com/{repo_slug}/issues/new?template=rewind.yml"

    if save.battle and save.battle.active:
        status = f"⚔️ 戦闘中: **{save.battle.name}**(ターン{save.battle.turn})"
    elif save.battle and save.battle.result == "victory":
        status = f"🏆 直前の戦い「{save.battle.name}」に**勝利**! 次のターン送信で新しい戦いが始まる。"
    elif save.battle and save.battle.result == "defeat":
        status = f"💀 「{save.battle.name}」で敗北……次のターン送信で再挑戦できる。"
    else:
        status = "🏕 拠点で休息中。ターンを送信すると冒険が始まる。"
    nemesis_name = str(((save.nemesis or {}).get("enemy") or {}).get("name", ""))
    if nemesis_name and not (save.battle and save.battle.active):
        status += f"\n\n👁 宿敵**{nemesis_name}**が待ち構えている——次の戦いで必ず現れる。"
    order = str((world.get("system_terms") or {}).get("world_order", "世界の理"))
    pr = (save.battle.pr_attack if save.battle else None) or {}
    if save.battle and save.battle.active and pr.get("status") in ("casting", "deadline"):
        num = pr.get("pr_number")
        left = max(0, int(pr.get("deadline_turn", 0)) - save.battle.turn)
        link = f"[PR #{num}](https://github.com/{repo_slug}/pull/{num})" if num else "PR"
        need = pr.get("break_need")
        how = f"ボスへ合計{need}ダメージで打ち破る" if need else "ボスを削って打ち破る"
        status += (
            f"\n\n🕳 **ボスが禁忌の詠唱中!** {link} が{order}を歪めようとしている(猶予**{left}ターン**)。"
            f"\n{how}か、**{link} を手動でクローズして封じる**かのどちらかを——放置すると強制マージされる。"
        )

    victories = save.stats.get("victories", 0)
    journal_tail = "\n".join(f"- {line}" for line in save.journal[-5:])
    spell_tokens = save.spell_tokens
    level = save.level
    xp = save.xp
    roster_count = len(save.roster_extra)

    return f"""# 🌠 {world_name}

*{tagline}*

GitHubだけで遊ぶソロRPG。**Issue=コントローラ、Actions=エンジン、README=画面、リポジトリ=セーブデータ。**

{status}

<!-- GAME:BOARD:BEGIN -->
{scene_block}![戦況ボード]({board_url})
<!-- GAME:BOARD:END -->

## 🎮 コマンド

| | |
|---|---|
| ▶ **[ターンを入力する]({new_turn_url})** | 4人の行動と対象を選んで送信(1フォーム=1ターン) |
| ⚡ **[全員通常攻撃(1タップ)]({all_normal_url})** | 全員「通常攻撃/自動」が入力済みのフォームが開く |
| ✨ **[技生成の儀式]({generate_url})** | 生成権(残り**{spell_tokens}**)を使い、詠唱文から新しい技を紡ぐ |
| 🔮 **[技アップデート]({update_url})** | 使い込んだ技の進化3案から選ぶ |
| ⏪ **[時戻しの儀式]({rewind_url})** | 生成権1を砕き、今の戦いの始まりへ時を巻き戻す(戦闘中のみ) |

送信後、数十秒でこのページのボードが更新される(結果はIssueにも返信される)。
現在: **Lv{level}**(XP {xp})/ 技生成権 **{spell_tokens}** / 控えメンバー {roster_count}人

## 📖 遊び方

1. 上のボードで各メンバーの**HP / {gauge_term} / 技のCT**を確認する
2. 「ターンを入力する」で行動(アビ1〜3・奥義・通常攻撃・待機)と対象を選ぶ
3. 各スロットの中身(技の名前と効果)はボードのチップに表示されている
4. CT中の技や、ゲージ不足の奥義を選ぶとエラー返信になり**ターンは消費されない**
5. 敵はヘイトが最も高い味方を狙う。タンクの挑発は敵の狙いを強制固定する

## 📜 旅の記録

{journal_tail}

これまでの勝利数: **{victories}**

---

<sub>ワールド1「{world_name}」/ 仕組みは [ARCHITECTURE.md](ARCHITECTURE.md)、進捗は [PROGRESS.md](PROGRESS.md) を参照。</sub>
"""
