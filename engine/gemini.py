"""Gemini画像生成境界(任意機能)。

GEMINI_API_KEY があれば、新しい敵の素材3枚(背景/胴体/可動パーツ)を生成して
assets/raw/ に置く(以後は通常の素材パイプラインが処理する)。
キーが無い・失敗した場合は何もしない(プレースホルダで続行。ゲームは止めない)。
キーやレスポンス全文はログに出さない。
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .models import Enemy

MODEL = "gemini-2.5-flash-image"
ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
TIMEOUT = 60

_LIGHTING = "光源は画面左上からの冷たい月光で統一する。"


def _prompts(enemy: Enemy, world: dict[str, Any]) -> dict[str, str]:
    view = str(world.get("worldview", ""))
    subject = f"「{enemy.name}({enemy.title})」— 性格は{enemy.personality or '不明'}の魔物"
    return {
        "background": (
            f"ファンタジーRPGの戦闘背景イラスト。{view} 夜の荒野または森。地平線は低めに。"
            f"キャラクターは描かない。{_LIGHTING} 横長。"
        ),
        "body": (
            f"ファンタジーRPGの敵モンスター{subject}の胴体イラスト(翼や腕など可動部は描かない)。"
            f"全身が収まる構図、単色の緑背景(#00FF00)のみ、影は落とさない。{_LIGHTING}"
        ),
        "part": (
            f"{subject}の可動パーツ(翼または大きな腕)のみを描いたイラスト。"
            f"パーツ単体、単色の緑背景(#00FF00)のみ、影は落とさない。{_LIGHTING}"
        ),
    }


class GeminiClient:
    def __init__(self, api_key: Optional[str] = None) -> None:
        self._key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")

    def available(self) -> bool:
        return bool(self._key)

    def _generate_image(self, prompt: str) -> bytes | None:
        payload = json.dumps(
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["IMAGE"]},
            }
        ).encode()
        req = urllib.request.Request(
            ENDPOINT,
            data=payload,
            method="POST",
            # キーはURLでなくヘッダで送る(例外メッセージ等にURLが含まれても漏れないように)
            headers={"Content-Type": "application/json", "x-goog-api-key": self._key},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
        for candidate in body.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                data = part.get("inlineData", {}).get("data")
                if data:
                    return base64.b64decode(data)
        return None

    def generate_enemy_assets(self, enemy: Enemy, world: dict[str, Any], raw_dir: str | Path) -> int:
        """素材を生成して保存する。成功枚数を返す(0=全滅。呼び出し側はプレースホルダで続行)。"""
        out = Path(raw_dir)
        out.mkdir(parents=True, exist_ok=True)
        ok = 0
        for name, prompt in _prompts(enemy, world).items():
            try:
                data = self._generate_image(prompt)
                if data:
                    (out / f"{name}.png").write_bytes(data)
                    ok += 1
                    print(f"gemini: {name} generated")
                else:
                    print(f"gemini: {name} returned no image")
            except Exception as e:  # キー・全文は出さない
                print(f"gemini: {name} failed ({type(e).__name__})")
        return ok
