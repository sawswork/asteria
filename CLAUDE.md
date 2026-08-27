# CLAUDE.md — 常時遵守事項

このリポジトリは「GitHub完結型AI生成RPG(ワールド1: アステリア)」。仕様の正は `REQUIREMENTS.md`、実装指示は `KICKOFF PROMPT.md`、決定事項は `DECISIONS.md`、進捗は `PROGRESS.md` を参照すること。

## 不変の制約(KICKOFF PROMPT.md より転記)

- エンジンコードに世界の固有名詞を書かない。世界の個性は world.json のみが持つ
- AIの出力は必ずJSONで受け、スクリプト検証を通過したものだけ採用する。効果=効果タグ辞書へのコンパイル、入力の自由文=スロットへのマッピング。AIが戦闘結果や数値を直接決めることは決してない(数値の最終決定権は常にスクリプト=予算制)
- ターン処理のAI呼び出しは1ターン1回に同梱する(敵AI判断+ログ文+演出指示)。モデルはconfigで切替可能にする(ターン処理=軽量モデル、生成イベント=上位モデル)
- 認証はサブスクリプションのみ: 実行時AIは CLAUDE_CODE_OAUTH_TOKEN で認証する。ANTHROPIC_API_KEY はSecretsにも環境にも一切設定しない(存在するとAPI従量課金が優先されてしまうため)
- 乱数は全てシードをセーブに記録し、リプレイ再現可能にする
- READMEに載せるSVGは自己完結(外部リソース参照禁止)。シーン≤1MB、ボード≤50KB
- Secretsやその値をログへ出さない。AIレスポンス全文もログに吐かない(要約のみ)
- workflow: concurrency は group固定・cancel-in-progress: false。permissions は最小限(contents: write, issues: write。M4のPR攻撃時のみ pull-requests: write を追加)
- 基準戦闘: 適正Lvパーティが適正ボスに8ターン前後で勝利し残HP約4割。バランス係数は balance.json に一元化し、後から調整可能にする

## 開発規約

- Python 3.12(ローカル3.11でも動くよう3.11互換で書く)。依存最小: Pillow / numpy / jsonschema / pytest(scipy可)
- 型ヒント必須。ロジックは純粋関数。I/O(git・GitHub API・AI呼び出し)は `engine/` 内の境界モジュールに隔離しモック差し替え可能にする
- 戦闘エンジンはCLIでローカル実行できること: `python -m engine.cli --input fixtures/turn.json`
- AI呼び出しは `--mock` で fixtures/ の固定JSON応答に差し替え可能。ユニットテストは全てモックで回す
- コミットは小さく頻繁に。壊れた状態でマイルストーンをまたがない
- 質問より決定+記録: 決めた事項は DECISIONS.md に1行ずつ残す
