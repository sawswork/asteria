"""README(ゲーム画面)の生成。

READMEは毎ターン全体を再生成する。ボード画像URLにはコミットSHAを付与してキャッシュを回避する。
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


def render_readme(save: Save, world: dict[str, Any], repo_slug: str, sha: str) -> str:
    world_name = str(world["world_name"])
    tagline = str(world.get("tagline", ""))
    if sha == "local":
        board_url = "assets/board.svg"  # ローカル生成・初期コミット用(閲覧中のブランチで解決される)
    else:
        board_url = f"https://raw.githubusercontent.com/{repo_slug}/{sha}/assets/board.svg"
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

    if save.battle and save.battle.active:
        status = f"⚔️ 戦闘中: **{save.battle.name}**(ターン{save.battle.turn})"
    elif save.battle and save.battle.result == "victory":
        status = f"🏆 直前の戦い「{save.battle.name}」に**勝利**! 次のターン送信で新しい戦いが始まる。"
    elif save.battle and save.battle.result == "defeat":
        status = f"💀 「{save.battle.name}」で敗北……次のターン送信で再挑戦できる。"
    else:
        status = "🏕 拠点で休息中。ターンを送信すると冒険が始まる。"

    victories = save.stats.get("victories", 0)
    journal_tail = "\n".join(f"- {line}" for line in save.journal[-5:])

    return f"""# 🌠 {world_name}

*{tagline}*

GitHubだけで遊ぶソロRPG。**Issue=コントローラ、Actions=エンジン、README=画面、リポジトリ=セーブデータ。**

{status}

<!-- ASTERIA:BOARD:BEGIN -->
![戦況ボード]({board_url})
<!-- ASTERIA:BOARD:END -->

## 🎮 コマンド

| | |
|---|---|
| ▶ **[ターンを入力する]({new_turn_url})** | 4人の行動と対象を選んで送信(1フォーム=1ターン) |
| ⚡ **[全員通常攻撃(1タップ)]({all_normal_url})** | 全員「通常攻撃/自動」が入力済みのフォームが開く |

送信後、数十秒でこのページのボードが更新される(結果はIssueにも返信される)。

## 📖 遊び方

1. 上のボードで各メンバーの**HP / 星光ゲージ / 技のCT**を確認する
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
