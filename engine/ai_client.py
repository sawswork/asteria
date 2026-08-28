"""AI呼び出し境界。

実行時は Claude Code CLI のヘッドレスモードをサブプロセス実行する:
  claude -p "<プロンプト>" --output-format json --model <config/ai.jsonのモデル>

- 認証は CLAUDE_CODE_OAUTH_TOKEN(サブスクリプション)のみ。ANTHROPIC_API_KEY は
  環境から必ず除去する(存在するとAPI従量課金が優先されるため)
- 応答はJSONとして抽出し、jsonschema検証を通過したものだけ返す。失敗はリトライ
  (config.max_retries)し、それでも駄目なら AiError → 呼び出し側がルール層へフォールバック
- Secretsや応答全文はログに出さない(種別・試行回数・結果種別のみ)

mock=True の場合は fixtures/ai/<kind>.json を返す(ユニットテストは常にモック)。
fixtureが配列なら呼び出し毎に順に消費する(リトライ挙動のテスト用)。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

try:
    import jsonschema
except ImportError:
    jsonschema = None  # type: ignore[assignment]

DEFAULT_CONFIG: dict[str, Any] = {
    "models": {"turn": "claude-haiku-4-5-20251001", "generation": "claude-sonnet-5"},
    "timeout_seconds": 60,
    "max_retries": 2,
}


class AiError(RuntimeError):
    """AI応答が得られない/検証を通らない。呼び出し側はルール層へフォールバックする。"""


def _extract_json(text: str) -> dict[str, Any]:
    """テキストから最初のJSONオブジェクトを取り出す。

    全文がJSONならそのまま。そうでなければ各 "{" 位置から raw_decode を試す
    (前置きの文章・コードフェンス・文字列内のバッククォート等に頑健)。
    """
    s = text.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = s.find("{", idx)
        if start < 0:
            break
        try:
            obj, _end = decoder.raw_decode(s[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        idx = start + 1
    raise AiError("応答にJSONオブジェクトが見つからない")


class AiClient:
    def __init__(
        self,
        mock: bool = False,
        fixtures_dir: str | Path = "fixtures/ai",
        config_path: str | Path = "config/ai.json",
    ) -> None:
        self.mock = mock
        self.fixtures_dir = Path(fixtures_dir)
        self._fixture_cursors: dict[str, int] = {}
        self.config = dict(DEFAULT_CONFIG)
        try:
            with open(config_path, encoding="utf-8") as f:
                self.config.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass

    # ---- モック -----------------------------------------------------------

    def _mock_response(self, kind: str) -> dict[str, Any]:
        path = self.fixtures_dir / f"{kind}.json"
        if not path.exists():
            raise AiError(f"モック応答がない: {kind}")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            cursor = self._fixture_cursors.get(kind, 0)
            if cursor >= len(data):
                cursor = len(data) - 1  # 最後の応答を返し続ける
            self._fixture_cursors[kind] = cursor + 1
            data = data[cursor]
        if not isinstance(data, dict):
            raise AiError(f"モック応答が不正: {kind}")
        return data

    # ---- 実呼び出し -------------------------------------------------------

    # サブプロセスに渡す環境は最小限(GITHUB_TOKEN等のSecretsをAI実行系に渡さない)
    _ENV_PASSTHROUGH = ("PATH", "HOME", "TMPDIR", "TERM", "LANG", "LC_ALL", "USER", "SHELL",
                        "CLAUDE_CODE_OAUTH_TOKEN", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                        "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS")

    def _invoke_cli(self, prompt: str, purpose: str) -> str:
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            raise AiError("CLAUDE_CODE_OAUTH_TOKEN が未設定")
        model = str(self.config["models"].get(purpose, self.config["models"]["turn"]))
        env = {k: os.environ[k] for k in self._ENV_PASSTHROUGH if k in os.environ}
        try:
            result = subprocess.run(
                [
                    "claude", "-p", prompt,
                    "--output-format", "json",
                    "--model", model,
                    # 1往復のテキスト生成のみ。ツールは全て禁止(プロンプト注入からの防御)
                    "--max-turns", "1",
                    "--disallowedTools",
                    "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit",
                ],
                capture_output=True,
                text=True,
                timeout=float(self.config["timeout_seconds"]),
                env=env,
            )
        except FileNotFoundError as e:
            raise AiError("claude CLI が見つからない") from e
        except subprocess.TimeoutExpired as e:
            raise AiError("claude CLI タイムアウト") from e
        if result.returncode != 0:
            raise AiError(f"claude CLI 異常終了(code={result.returncode})")
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise AiError("claude CLI の出力がJSONではない") from e
        if envelope.get("is_error"):
            raise AiError("claude CLI がエラー応答を返した")
        text = envelope.get("result")
        if not isinstance(text, str) or not text:
            raise AiError("claude CLI 応答に result がない")
        return text

    # ---- 公開API ----------------------------------------------------------

    def call(
        self,
        kind: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
        purpose: str = "generation",
    ) -> dict[str, Any]:
        """スキーマ検証済みのJSON応答を返す。失敗はリトライ後 AiError。

        kind はモック応答ファイル名(fixtures/ai/<kind>.json)とログ種別に使う。
        """
        attempts = int(self.config["max_retries"]) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                if self.mock:
                    obj = self._mock_response(kind)
                else:
                    obj = _extract_json(self._invoke_cli(prompt, purpose))
                if schema is not None and jsonschema is not None:
                    jsonschema.validate(obj, schema)
                print(f"ai: {kind} ok (attempt {attempt + 1})")
                return obj
            except AiError as e:
                last_error = e
                print(f"ai: {kind} failed (attempt {attempt + 1}): {e}")
            except Exception as e:  # jsonschema.ValidationError など(応答全文は出さない)
                last_error = e
                print(f"ai: {kind} invalid (attempt {attempt + 1}): {type(e).__name__}")
        raise AiError(f"{kind}: {attempts}回失敗(最終: {type(last_error).__name__})")
