"""Одноразові коди для дій у два кроки: витрати Gemini і видалення рендерів.

Крок 1 повертає опис дії й код; Claude мусить показати опис людині й дочекатися згоди.
Крок 2 виконує дію лише з тим самим кодом, для тієї самої дії й цілі, протягом 10 хв.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

TTL = 600
MAX_WRONG = 5


class CodeError(Exception):
    pass


@dataclass
class Pending:
    action: str
    target: int
    amount: float
    expires: float


class Codes:
    def __init__(self, clock=time.monotonic):
        self._items: dict[str, Pending] = {}
        self._wrong = 0
        self._clock = clock

    def issue(self, action: str, target: int, amount: float = 0.0) -> str:
        now = self._clock()
        self._items = {c: p for c, p in self._items.items() if p.expires > now and (p.action, p.target) != (action, target)}
        code = f"{secrets.randbelow(10 ** 6):06d}"
        self._items[code] = Pending(action, target, amount, now + TTL)
        return code

    def take(self, code: str, action: str, target: int) -> Pending:
        p = self._items.get(code.strip())
        if p is None or p.expires <= self._clock() or (p.action, p.target) != (action, target):
            self._wrong += 1
            if self._wrong >= MAX_WRONG:  # схоже на перебір — анулюємо всі коди
                self._items.clear()
                self._wrong = 0
            raise CodeError("Код невірний або прострочений — почни з першого кроку ще раз")
        del self._items[code.strip()]
        self._wrong = 0
        return p
