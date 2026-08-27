"""AI呼び出し境界(M1ではダミー)。

M2以降: GitHub Actions 内で Claude Code CLI を CLAUDE_CODE_OAUTH_TOKEN(サブスクリプション認証)で
サブプロセス実行し、`claude -p "<プロンプト>" --output-format json` の結果JSONを受け取る。
ANTHROPIC_API_KEY は決して設定しない。応答はスキーマ検証を通過したものだけ採用し、
失敗時はリトライ2回→ルール層フォールバック。全文はログに出さない(要約のみ)。

mock=True の場合は fixtures/ai/ の固定JSONを返す(ユニットテストは常にモック)。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class AiClient:
    def __init__(self, mock: bool = False, fixtures_dir: str | Path = "fixtures/ai") -> None:
        self.mock = mock
        self.fixtures_dir = Path(fixtures_dir)

    def call(self, kind: str, prompt: str) -> dict[str, Any]:
        """kind はモック応答ファイル名(fixtures/ai/<kind>.json)に対応する。"""
        if self.mock:
            path = self.fixtures_dir / f"{kind}.json"
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        raise NotImplementedError("実AI呼び出しはM2で実装する(claude CLI ヘッドレスモード)")
