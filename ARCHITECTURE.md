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

## M4のギミック(実装の置き場所)

- **残留タグ+チェイン反応**: `battle.py` の `_attach_field` / `_field_and_chain_mult`。反応表と歪み弱点プールは `world.json`(`chain_reactions` / `distortion_weaknesses`)、係数上限は `balance.json`。damage効果の `"field"` 添えタグが反応をトリガーする
- **誓約(制約タグ)**: 予算乗算は `spells.constraint_multiplier`、発動条件の実行時検証は `commands._constraint_violations`、代償(自己スタン)と使用回数は `battle.py`。しきい値は全て `balance.constraints`
- **適応進化**: 予告は `battle._check_evolution_triggers`(ターン終了時)、実体化は `_resolve_pending_evolutions`(次ターン冒頭)。演出のみAI(`generation.generate_evolution`、予算検証あり)、数値ボーナスと歪み弱点はスクリプト+セーブ済み乱数
- **歴史の共鳴**: `battle._detect_resonance`(技IDの `_genN` 世代で判定)。増幅率=初代技コストに対する現在予算の比(上限は `balance.resonance.amp_cap`)
- **フルオート**: `turn_runner._auto_commands`(決定的採択)+ `_handle_turn` のループ。自由記述「フルオート N」で発動、上限 `balance.full_auto_max_turns`
- **宿敵**: 敗北時に `Save.nemesis` へ敵の全状態を保存(`battle.resolve_turn`)、再戦の再構築は `battle.nemesis_enemy`、撃破で解消
- **時戻し**: `turn_runner._handle_rewind`。git履歴(`gitops.history_for_path` / `show_file` / `list_files`)から現在の戦闘の最古のコミットを特定し、save/ を一時ディレクトリ経由で復元して新しいコミットとして積む(履歴改変なし)
- **PR攻撃**: 状態機械は `battle._check_pr_attack`(純粋)、実PRの作成・監視・強制マージ・後始末は `turn_runner._process_pr_attack`(I/O境界、`gh_api` のPRメソッド使用)。`battle_override.json` は `_merged_balance` が balance に深マージし、戦闘終了時に撤去

## 記録の二層構造(log.md と chronicle/)

冒険の記録は目的の違う2つを並行して残す。

- `save/log.md`(旅の記憶)— **目次**。1行サマリだけを積み、AIプロンプトに同梱する。
  `balance.journal_max_entries`(200)で上限があり、古いものから落ちる
- `save/chronicle/chapter-NNN.md`(年代記)— **本文**。要約せず全文を残す。上限なし。
  最後に1冊の書籍へ編むための素材で、AIプロンプトには載せない

章立ては戦闘単位(`stats.chapters` が戦闘開始で加算される)。1つの章に
「その戦いの全ターンのログ」と「その後の拠点での出来事(技生成の詠唱文・誓約・
アップデート・時戻し)」が時系列で入り、末尾に勝敗が刻まれる。

書き込みは **Issue番号をマーカー(`<!-- issue:N -->`)にした冪等な置換**で行う。
push競合のリプレイでは同じIssueが何度も再解決されるため、素朴な追記だと本文が二重になる。
置換は位置を保つので、再処理しても時系列は崩れない。純粋な文字列操作は `engine/chronicle.py`、
ファイルI/Oは `turn_runner._write_chronicle` にある。

## 書籍化(book/)

`[BOOK]` フォームで年代記を1冊へ編む。素材は年代記(本文)・魔導書(`save/spells/`)・
log.md(年表)の3つで、いずれもリポジトリ内に既にあるものだけを使う。

- `book/chapters/chapter-NNN.md` — 章ごとの語り(キャッシュ)。先頭に素材のハッシュを持ち、
  章が伸びた時だけ編み直す
- `book/journey.md` — 表題・序文・各章・魔導書・年表を綴じた1冊

1回の実行で編む章数は `balance.book.max_ai_chapters_per_run` で抑える。章が増えても
ジョブ時間が伸びず、続きは再実行で編める。AIが使えなかった章は記録そのものを載せるので、
書物に欠落は生じない。編纂はセーブを変更しない。
