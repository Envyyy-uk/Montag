"""Де ставити ефектні переходи: лише на акцентах, 80–90% склейок лишаються різкими."""

from __future__ import annotations

import random

from edit.timeline import Timeline
from render.transitions import TRANSITIONS, edge_seconds

PRIORITY = {"drop": 0, "insert": 1, "chorus": 2}


def plan(tl: Timeline, allowed: list[str], max_share: float = 0.15, disabled: set[str] | None = None,
         seed: int = 0) -> list[dict]:
    kinds = [k for k in allowed if k in TRANSITIONS and k not in (disabled or set())]
    cuts = len(tl.clips) - 1
    if not kinds or cuts <= 0:
        return []
    half = 0.5 / tl.fps
    accents: dict[int, int] = {}
    times = [(m.t, PRIORITY.get(m.kind, 3)) for m in tl.markers] + [(i.end, PRIORITY["insert"]) for i in tl.inserts]
    for i in range(1, len(tl.clips)):
        t = tl.clips[i].start
        pr = [p for at, p in times if abs(at - t) <= half]
        if pr:
            accents[i] = min(pr)
    budget = int(max_share * cuts)
    if accents and budget == 0:
        budget = 1
    rng = random.Random(seed)
    order = sorted(accents, key=lambda i: (accents[i], i))
    chosen: list[dict] = []
    last_kind = None
    for i in order:
        if len(chosen) >= budget:
            break
        a, b = tl.clips[i - 1], tl.clips[i]
        pool = kinds[:]
        rng.shuffle(pool)
        pool.sort(key=lambda k: k == last_kind)  # не той самий перехід двічі поспіль
        for k in pool:
            t_out, t_in = edge_seconds(k, tl.beat, a.duration, b.duration)
            needs_out = TRANSITIONS[k].out_beats > 0
            if needs_out and t_out * tl.fps < 2:
                continue  # кліп закороткий для цього ефекту
            if TRANSITIONS[k].structural and a.kind == "insert":
                continue  # живу вставку не заморожуємо і не перемотуємо — там звук натовпу
            chosen.append({"cut": i, "kind": k, "t": b.start, "out": round(t_out, 4), "in": round(t_in, 4)})
            last_kind = k
            break
    return sorted(chosen, key=lambda c: c["cut"])
