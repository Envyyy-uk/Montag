"""Ключові кадри гучності треку для DaVinci: та сама крива приглушення, що й у mp4."""

from __future__ import annotations

from edit.audio_mix import RAMP


def duck_keyframes(inserts: list[tuple[float, float, float]], base_db: float = 0.0,
                   ramp: float = RAMP) -> list[tuple[float, float]]:
    """(початок, кінець, глибина дБ) вставок → [(с від початку кліпу треку, дБ)].
    Вставки розділені ≥ 8 с, тож рампи не перетинаються; точки лише зростають у часі."""
    keys: list[tuple[float, float]] = [(0.0, base_db)]
    for s, e, depth in sorted(inserts):
        for t, db in ((max(0.0, s - ramp), base_db), (s, base_db + depth), (e, base_db + depth),
                      (e + ramp, base_db)):
            if t > keys[-1][0] + 1e-6:
                keys.append((round(t, 4), round(db, 2)))
    return keys
