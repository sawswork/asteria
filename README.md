# 🌠 アステリア

*砕けた星々の欠片が空を巡る世界*

GitHubだけで遊ぶソロRPG。**Issue=コントローラ、Actions=エンジン、README=画面、リポジトリ=セーブデータ。**

⚔️ 戦闘中: **蝕まれた岩甲獣との戦い**(ターン3)

<!-- GAME:BOARD:BEGIN -->
![戦闘シーン](assets/scene.svg?v=i31-a0)

![戦況ボード](assets/board.svg?v=i31-a0)
<!-- GAME:BOARD:END -->

## 🎮 コマンド

| | |
|---|---|
| ▶ **[ターンを入力する](https://github.com/sawswork/asteria/issues/new?template=turn.yml)** | 4人の行動と対象を選んで送信(1フォーム=1ターン) |
| ⚡ **[全員通常攻撃(1タップ)](https://github.com/sawswork/asteria/issues/new?template=turn.yml&attacker_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83&attacker_target=%E8%87%AA%E5%8B%95&support_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83&support_target=%E8%87%AA%E5%8B%95&tank_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83&tank_target=%E8%87%AA%E5%8B%95&healer_action=%E9%80%9A%E5%B8%B8%E6%94%BB%E6%92%83&healer_target=%E8%87%AA%E5%8B%95)** | 全員「通常攻撃/自動」が入力済みのフォームが開く |
| ✨ **[技生成の儀式](https://github.com/sawswork/asteria/issues/new?template=generate.yml)** | 生成権(残り**1**)を使い、詠唱文から新しい技を紡ぐ |
| 🔮 **[技アップデート](https://github.com/sawswork/asteria/issues/new?template=update.yml)** | 使い込んだ技の進化3案から選ぶ |
| ⏪ **[時戻しの儀式](https://github.com/sawswork/asteria/issues/new?template=rewind.yml)** | 生成権1を砕き、今の戦いの始まりへ時を巻き戻す(戦闘中のみ) |

送信後、数十秒でこのページのボードが更新される(結果はIssueにも返信される)。
現在: **Lv4**(XP 143)/ 技生成権 **1** / 控えメンバー 2人

## 📖 遊び方

1. 上のボードで各メンバーの**HP / 星光ゲージ / 技のCT**を確認する
2. 「ターンを入力する」で行動(アビ1〜3・奥義・通常攻撃・待機)と対象を選ぶ
3. 各スロットの中身(技の名前と効果)はボードのチップに表示されている
4. CT中の技や、ゲージ不足の奥義を選ぶとエラー返信になり**ターンは消費されない**
5. 敵はヘイトが最も高い味方を狙う。タンクの挑発は敵の狙いを強制固定する

## 📜 旅の記録

- リュノが新しい技「誓ひの濡雷閃」を紡いだ(旧「星の勇歌」)
- 「蝕まれた岩甲獣との戦い」に勝利(ターン4)
- パーティがLv4に到達
- 「夜哭きの梟との戦い」に勝利(ターン5)
- 流れ星の旅人が仲間に加わった

これまでの勝利数: **6**

---

<sub>ワールド1「アステリア」/ 仕組みは [ARCHITECTURE.md](ARCHITECTURE.md)、進捗は [PROGRESS.md](PROGRESS.md) を参照。</sub>
