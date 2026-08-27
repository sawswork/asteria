"""決定的乱数。

seed と counter をセーブに記録し、同じセーブからは常に同じ乱数列を再現する
(リプレイ再現性・テストの決定性・AGI同値のタイブレークに使用)。
counter-based 方式(SHA-256(seed:counter))なので fast-forward が不要。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence, TypeVar

T = TypeVar("T")


@dataclass
class Rng:
    seed: int
    counter: int = 0

    def _next_float(self) -> float:
        """[0, 1) の一様乱数を1つ取り出し、counter を進める。"""
        digest = hashlib.sha256(f"{self.seed}:{self.counter}".encode()).digest()
        self.counter += 1
        return int.from_bytes(digest[:8], "big") / 2**64

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self._next_float()

    def randint(self, low: int, high: int) -> int:
        """low..high(両端含む)の一様整数。"""
        return low + int(self._next_float() * (high - low + 1))

    def choice(self, seq: Sequence[T]) -> T:
        if not seq:
            raise ValueError("empty sequence")
        return seq[int(self._next_float() * len(seq)) % len(seq)]
