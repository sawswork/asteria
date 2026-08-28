# 🌠 アステリア

*砕けた星々の欠片が空を巡る世界*

GitHubだけで遊ぶソロRPG。**Issue=コントローラ、Actions=エンジン、README=画面、リポジトリ=セーブデータ。**

⚔️ 戦闘中: **星蝕の仔狼との遭遇**(ターン8)

<!-- GAME:BOARD:BEGIN -->
![戦況ボード](assets/board.svg?v=t7-i8)
<!-- GAME:BOARD:END -->

## 🎮 コマンド

| | |
|---|---|
| ▶ **[ターンを入力する](https://github.com/sawswork/asteria/issues/new?template=turn.yml)** | 4人の行動と対象を選んで送信(1フォーム=1ターン) |
| ⚡ **[全員通常攻撃(1タップ)](https://github.com/sawswork/asteria/issues/new?template=turn.yml&attacker_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83&attacker_target=%E8%87%AA%E5%8B%95&support_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83&support_target=%E8%87%AA%E5%8B%95&tank_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83&tank_target=%E8%87%AA%E5%8B%95&healer_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83&healer_target=%E8%87%AA%E5%8B%95)** | 全員「通常攻撃/自動」が入力済みのフォームが開く |

送信後、数十秒でこのページのボードが更新される(結果はIssueにも返信される)。

## 📖 遊び方

1. 上のボードで各メンバーの**HP / 星光ゲージ / 技のCT**を確認する
2. 「ターンを入力する」で行動(アビ1〜3・奥義・通常攻撃・待機)と対象を選ぶ
3. 各スロットの中身(技の名前と効果)はボードのチップに表示されている
4. CT中の技や、ゲージ不足の奥義を選ぶとエラー返信になり**ターンは消費されない**
5. 敵はヘイトが最も高い味方を狙う。タンクの挑発は敵の狙いを強制固定する

## 📜 旅の記録

- 旅が始まった。

これまでの勝利数: **0**

---

<sub>ワールド1「アステリア」/ 仕組みは [ARCHITECTURE.md](ARCHITECTURE.md)、進捗は [PROGRESS.md](PROGRESS.md) を参照。</sub>
