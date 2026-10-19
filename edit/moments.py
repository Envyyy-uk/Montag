"""Моменти для вертикальних роликів: кожен ролик 15–25 с навколо одного сильного моменту.

Пік — шматок з найвищою оцінкою, підсилений сплеском натовпу і придатністю для вертикалі.
Структура ролика: розгін (безперервний матеріал перед піком у тому ж відео, а якщо його мало —
інші сильні шматки, що були раніше), пік, 1–2 с після піку. Ролики не перетинаються за
матеріалом і по можливості беруться з різних частин події.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from edit.select import Shot

TAIL = 1.5
MIN_LEN, MAX_LEN = 15.0, 25.0
CONTIGUOUS = 0.05   # шматки ближче за це — одна безперервна зйомка


@dataclass
class Piece:
    media_id: int
    src_in: float
    duration: float
    role: str                  # buildup | peak | tail
    subject_x: float | None = None


@dataclass
class Moment:
    peak: Shot
    pieces: list[Piece] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return sum(p.duration for p in self.pieces)


def peak_value(s: Shot, tag_weights: dict[str, float] | None = None) -> float:
    v = s.score / 10 + 0.3 * min(1.0, s.burst / 15)
    v += sum((tag_weights or {}).get(t, 0.0) for t in s.tags)  # правки словами: «більше натовпу»
    if s.vertical_ok:
        v += 0.1
    elif s.vertical_ok is False:
        v -= 0.1
    return v


def _key(s: Shot) -> tuple[int, float]:
    return (s.media_id, round(s.start, 3))


def _run_before(pool_by_media: dict[int, list[Shot]], peak: Shot, used: set) -> list[Shot]:
    """Безперервні придатні шматки того ж відео прямо перед піком (від найближчого)."""
    out, cur = [], peak
    for s in reversed([x for x in pool_by_media.get(peak.media_id, []) if x.end <= peak.start + 1e-6]):
        if abs(s.end - cur.start) > CONTIGUOUS or _key(s) in used:
            break
        out.append(s)
        cur = s
    return out


def _tail_after(pool_by_media: dict[int, list[Shot]], peak: Shot, used: set) -> Shot | None:
    for s in pool_by_media.get(peak.media_id, []):
        if abs(s.start - peak.end) <= CONTIGUOUS and _key(s) not in used:
            return s
    return None


def build_moment(peak: Shot, pool: list[Shot], used: set, length: float) -> Moment:
    by_media: dict[int, list[Shot]] = {}
    for s in sorted(pool, key=lambda s: (s.media_id, s.start)):
        by_media.setdefault(s.media_id, []).append(s)
    m = Moment(peak)
    taken = {_key(peak)}
    tail_shot = _tail_after(by_media, peak, used)
    tail = min(TAIL, tail_shot.length) if tail_shot else 0.0
    if tail_shot:
        taken.add(_key(tail_shot))
    need = length - peak.length - tail

    # розгін: спершу безперервна зйомка перед піком
    buildup: list[Piece] = []
    for s in _run_before(by_media, peak, used):
        if need <= 0.05:
            break
        d = min(s.length, need)
        buildup.insert(0, Piece(s.media_id, s.end - d, d, "buildup", s.subject_x))
        taken.add(_key(s))
        need -= d
    # далі — найкращі ранніші шматки з інших місць; якщо їх замало — найближчі за часом
    if need > 0.05:
        free = [s for s in pool if _key(s) not in used | taken]
        earlier = sorted((s for s in free if s.chrono < peak.chrono), key=lambda s: -s.score)
        nearest = sorted((s for s in free if s.chrono >= peak.chrono), key=lambda s: abs(s.chrono - peak.chrono))
        chosen = []
        for s in earlier + nearest:
            if need <= 0.05:
                break
            d = min(s.length, 4.0, need)
            if d < 1.0 and need > 1.0:
                continue
            chosen.append((s, d))
            taken.add(_key(s))
            need -= d
        chosen.sort(key=lambda x: x[0].chrono)
        buildup = [Piece(s.media_id, s.start + (s.length - d) / 2, d, "buildup", s.subject_x) for s, d in chosen] + buildup
    m.pieces = buildup + [Piece(peak.media_id, peak.start, peak.length, "peak", peak.subject_x)]
    if tail_shot:
        m.pieces.append(Piece(tail_shot.media_id, tail_shot.start, tail, "tail", tail_shot.subject_x))
    used |= taken
    # злити суміжні шматки одного відео — це одна безперервна зйомка, склейки там не потрібні
    merged: list[Piece] = []
    for p in m.pieces:
        last = merged[-1] if merged else None
        if last and last.media_id == p.media_id and abs(last.src_in + last.duration - p.src_in) <= CONTIGUOUS:
            last.duration = p.src_in + p.duration - last.src_in
            last.role = "peak" if "peak" in (last.role, p.role) else last.role
        else:
            merged.append(Piece(p.media_id, p.src_in, p.duration, p.role, p.subject_x))
    m.pieces = merged
    return m


def choose(pool: list[Shot], count: int = 3, length: float = 20.0,
           tag_weights: dict[str, float] | None = None) -> list[Moment]:
    length = min(MAX_LEN, max(MIN_LEN, length))
    # матеріалу на всі ролики заданої довжини не вистачає — коротшаємо до 15 с, щоб влізло більше повноцінних
    total = sum(s.length for s in pool)
    if count > 1 and total < count * length:
        length = max(MIN_LEN, min(length, 0.9 * total / count))
    used: set = set()
    moments: list[Moment] = []
    sep = 1 / (2 * max(1, count))
    for peak in sorted(pool, key=lambda s: -peak_value(s, tag_weights)):
        if len(moments) >= count:
            break
        if _key(peak) in used:
            continue
        if any(abs(peak.chrono - m.peak.chrono) < sep for m in moments):
            continue  # бажано з різних частин події
        moments.append(build_moment(peak, pool, used, length))
    # матеріалу на різні частини не вистачило — беремо, що є
    for peak in sorted(pool, key=lambda s: -peak_value(s, tag_weights)):
        if len(moments) >= count:
            break
        if _key(peak) not in used:
            moments.append(build_moment(peak, pool, used, length))
    # коли матеріал вичерпано, хвостові моменти виходять куцими — краще менше роликів, ніж ролик на 7 с
    full = [m for m in moments if m.duration >= MIN_LEN - 0.5]
    if not full and moments:
        full = [max(moments, key=lambda m: m.duration)]
    return sorted(full, key=lambda m: m.peak.chrono)
