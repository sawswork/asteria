# ARCHITECTURE.md

GitHub完結型AI生成RPGの構成。仕様の正は `REQUIREMENTS.md`(v0.1)、進め方は `KICKOFF PROMPT.md`。

## リポジトリ構成

```
CLAUDE.md               常時遵守事項(不変の制約)
ARCHITECTURE.md         本書
PROGRESS.md             マイルストーン進捗チェックリスト
DECISIONS.md            決定事項ログ(1行1決定)
README.md               ゲーム画面(戦況ボード+操作リンク)。ゲーム側が自動更新する
world/
  world.json            世界定数(世界名・世界観・用語・敵図鑑・フォールバック敵)。固有名詞はここだけ
  balance.json          バランス係数の一元管理(ダメージ式・ヘイト・ゲージ・技予算・敵スケール・レベル曲線)
config/
  ai.json               AIモデル設定(ターン処理=軽量 / 生成=上位)とリトライ回数
engine/                 ゲームエンジン(Python パッケージ。世界の固有名詞を含まない)
  models.py             型定義(Actor/Ability/BattleState/SaveData 等)+ save⇔dict 変換
  rng.py                シード+カウンタ方式の決定的乱数(セーブに記録しリプレイ可能)
  commands.py           スロット語彙の定義、コマンド検証(不正手検知)、対象解決
  battle.py             純粋関数の戦闘解決(AGI順・ヘイト・挑発ロック・CT・奥義ゲージ)
  enemy_ai.py           敵AIルール層(ヘイト最大狙い・挑発ロック遵守)。M2で知能層を追加
  board.py              戦況ボードSVG生成(自己完結・50KB以内)
  screen.py             README(ゲーム画面)のマーカー区間更新
  issue_parser.py       Issue Form本文(Markdown)→各フォームの入力(TURN/GENERATE/UPDATE)
  save_io.py            セーブの読み書き境界(スキーマv2: 分割ファイル+v1透過移行)
  spells.py             技の予算計算・効果タグ辞書スキーマ・検証(数値の最終決定権はここ)
  ai_schemas.py         AI応答のJSONスキーマ(技生成/3案/敵生成/ターン/勧誘)
  prompts.py            AIプロンプト構築(純粋関数。世界観+旅の記憶を同梱)
  ai_client.py          AI呼び出し境界(claude CLI ヘッドレス+スキーマ検証+リトライ)。--mock でfixtures固定応答
  turn_ai.py            ターン処理のAI同梱呼び出し(敵知能層判断+ログ味付け。失敗=ルール層)
  generation.py         生成系オーケストレーション(技/3案/敵/勧誘。検証→却下なら再生成→フォールバック)
  assets.py             素材パイプライン(クロマキー→連結成分分離→WebP+サイズ予算の梯子→manifest)
  scene.py              シーンSVG(戦闘開始時のみ。素材base64内包 or プレースホルダ。SMIL演出)
  gemini.py             Gemini画像生成境界(任意。GEMINI_API_KEYがあれば敵素材3枚を自動生成)
  gh_api.py             GitHub REST境界(コメント投稿・Issueクローズ)。GITHUB_TOKEN使用
  gitops.py             git操作境界(add/commit/push、SHA取得)
  turn_runner.py        Actionsエントリ: イベント→検証→解決→保存→画面→返信→クローズ
  cli.py                ローカル実行: python -m engine.cli --input fixtures/turn.json
save/                   セーブ(スキーマv2)
  state.json            乱数・戦闘状態・処理済みIssue・統計
  player.json           レベル・XP・技生成権・編成
  party/<id>.json       メンバー1人1ファイル(スロット=技ID参照)
  spells/<id>.json      技1つ1ファイル(差し替え後も残る=魔導書・成長史)
  log.md                旅の記憶(AIプロンプトに同梱)
assets/
  board.svg             最新の戦況ボード(毎ターン再生成・直近ターンのSMILリプレイ付き)
  scene.svg             戦闘シーン(戦闘開始時のみ再生成。素材内包 or プレースホルダ)
  raw/                  素材の入力置き場(単色緑背景画像。置くとassets.ymlが合成)
  parts/                加工済み素材(WebP+manifest.json)
fixtures/               CLI/テスト用の固定入力(ターンJSON・Issue本文サンプル・AIモック応答)
tests/                  pytest(全てAIモックで実行)
.github/
  ISSUE_TEMPLATE/turn.yml       ターン入力フォーム(固定YAML・スロット語彙)
  ISSUE_TEMPLATE/generate.yml   技生成の儀式フォーム(生成権を消費・詠唱文)
  ISSUE_TEMPLATE/update.yml     技アップデートフォーム(3案提示→選択の2段階)
  workflows/turn.yml            Issue処理ワークフロー([TURN]/[GENERATE]/[UPDATE] 共通・直列化)
  workflows/ci.yml              push時に pytest を実行
  workflows/assets.yml          assets/raw/ へのpushで素材パイプライン→シーン再合成
  workflows/tag.yml             マイルストーンタグ付け(workflow_dispatch)
```

## データフロー(1ターン)

```
プレイヤー(スマホのGitHubアプリ)
  └─ Issue Form「ターン入力」送信 (固定ドロップダウン: アビ1/アビ2/アビ3/奥義/通常攻撃/待機 × 敵1〜3/各役割/自動)
       └─ GitHub Actions turn.yml 起動
            ├─ ガード: Issue作成者==リポジトリオーナー / [TURN]タイトル / concurrencyで直列化
            ├─ checkout: ref=デフォルトブランチ(イベントSHAは古い可能性があるため常に最新先端)
            └─ engine.turn_runner(オープンな[TURN] Issueを番号順に全処理=取りこぼし回収)
                 ├─ issue_parser: フォーム本文 → コマンドJSON(既知ラベルのみ区切り・初出優先)
                 ├─ save_io: save/state.json 読込(処理済みIssueなら冪等スキップ)
                 ├─ commands: 検証。不正手 → エラー返信+Issueクローズ+ターン不消費(セーブ無変更)
                 ├─ battle.resolve_turn(純粋関数): AGI順に行動解決 → 新状態+ログ
                 ├─ save_io: セーブ書込 / board: assets/board.svg 生成
                 ├─ screen: README更新(ボードは相対URL+?v=t{ターン}-i{Issue}のキャッシュ回避クエリ)
                 ├─ gitops: save+board+READMEを1コミット → push
                 │    └─ push拒否時: fetch+reset --hardでリモート先端に戻し、ターン全体を再解決(最大3回)
                 └─ gh_api: 結果コメント投稿 → Issueクローズ
README(ゲーム画面)に新しい戦況ボードが表示される
```

## モジュール分割の原則

- **純粋関数コア**: `battle.py` / `commands.py` / `enemy_ai.py` / `board.py` / `screen.py` は入力→出力のみ。時刻・乱数・I/Oに直接触れない(乱数は `rng.py` の状態をセーブ経由で受け渡す)
- **境界モジュール**: `save_io` / `gh_api` / `gitops` / `ai_client` だけが外界に触れる。テストではすべて差し替え
- **世界データ分離**: エンジンは `world.json` / `balance.json` / `save/` を読むだけ。固有名詞・数値バランスはデータ側
- **スロット語彙は不変**: 行動=`アビ1/アビ2/アビ3/奥義/通常攻撃/待機`、対象=`敵1/敵2/敵3/アタッカー/サポート/タンク/ヒーラー/自動`。フォームYAMLは書き換えない。各スロットの中身はREADMEのボードが表示する

## 乱数とリプレイ

セーブに `rng.seed` と `rng.counter` を保持。乱数は SHA-256(seed, counter) から導出する counter-based 方式で、
セーブ時点から常に同じ列を再現できる(ターンのリプレイ・テストの決定性・同値AGIのタイブレークに使用)。

## AI呼び出し(M2以降)

`ai_client.py` に隔離。Actions内で Claude Code CLI をインストールし、`CLAUDE_CODE_OAUTH_TOKEN` の下で
`claude -p "<プロンプト>" --output-format json` をサブプロセス実行する。応答はJSONスキーマ検証を通過したものだけ採用。
`--mock` 時は `fixtures/ai/*.json` を返す。M1はルール層のみでAI呼び出しゼロ。

## 冪等性・直列化

- ワークフロー: `concurrency: { group: asteria-turn, cancel-in-progress: false }` で1ターンずつ処理
- セーブに `processed_issues` を記録。同じIssueが再処理されても状態は変わらない(連投してもセーブが壊れない)
- エンジンはOpen状態のIssueのみ処理し、処理完了時にクローズする
