"""Вибір кадрів у слоти ритму.

Для кожного слота береться найкращий доступний шматок з урахуванням:
- оцінки (Gemini або локальної);
- різноманітності: не та сама сцена/відео поспіль, не схожі теги, баланс камер;
- хронології — де це не шкодить ритму (м'який штраф, а не правило);
- енергії: на дропах — рух, натовп, сцена; у тихих місцях — спокійніші кадри.
Кадр з оцінкою 9–10 може тримати до 2 тактів (8 бітів) навіть на дропі.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from edit.rhythm import Slot

HERO_SCORE = 9.0
HERO_MAX_BEATS = 8
HERO_TOP_SHARE = 0.10   # «герой» — лише з верхніх 10% матеріалу
HERO_COOLDOWN = 16      # бітів між героями (4 такти), щоб ритм не «застигав»
ENERGY_TAGS = {"натовп", "сцена", "світло", "артист", "рух"}


@dataclass
class Shot:
    media_id: int
    start: float
    end: float
    score: float               # 0–10
    source: str = "local"
    tags: list[str] = field(default_factory=list)
    scene: tuple[int, int] = (0, 0)
    camera: str = ""
    chrono: float = 0.0        # позиція в події 0–1
    motion: float = 0.0
    used: int = 0
    burst: float = 0.0          # сплеск натовпу, дБ над фоном
    vertical_ok: bool | None = None
    subject_x: float | None = None

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class Weights:
    score: float = 1.0
    same_scene: float = 0.5
    same_media: float = 0.15
    similar_tags: float = 0.15
    chronology: float = 0.3
    camera_balance: float = 0.2
    energy_match: float = 0.1
    reuse: float = 0.6
    tag_weights: dict[str, float] = field(default_factory=dict)  # правки словами: «більше натовпу»


@dataclass
class Choice:
    slot: Slot
    shot: Shot
    src_in: float
    hero: bool = False
    reused: bool = False


def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def value(shot: Shot, slot: Slot, prev: Shot | None, pos: float, cam_share: dict[str, float],
          n_cams: int, w: Weights) -> float:
    v = w.score * shot.score / 10
    v += sum(w.tag_weights.get(t, 0.0) for t in shot.tags)
    if prev is not None:
        if shot.scene == prev.scene:
            v -= w.same_scene
        elif shot.media_id == prev.media_id:
            v -= w.same_media
        v -= w.similar_tags * _jaccard(shot.tags, prev.tags)
    v -= w.chronology * abs(shot.chrono - pos)
    if n_cams > 1:
        v -= w.camera_balance * max(0.0, cam_share.get(shot.camera, 0.0) - 1 / n_cams)
    lively = min(1.0, shot.motion / 20)
    if slot.level == "high":
        v += w.energy_match * (lively + (0.5 if ENERGY_TAGS & set(shot.tags) else 0))
    elif slot.level == "low":
        v += w.energy_match * (1 - lively)
    if shot.used:
        v -= w.reuse * shot.used
    return v


def choose(slots: list[Slot], pool: list[Shot], total: float, w: Weights | None = None) -> list[Choice]:
    """Жадібний вибір по слотах. Слот, для якого немає досить довгого шматка, ділиться навпіл по біту."""
    w = w or Weights()
    work = list(slots)
    out: list[Choice] = []
    cams: dict[str, float] = {}
    n_cams = len({s.camera for s in pool}) or 1
    prev: Shot | None = None
    last_hero_beat = -10**6
    scores = sorted((s.score for s in pool), reverse=True)
    hero_floor = max(HERO_SCORE, scores[max(0, int(len(scores) * HERO_TOP_SHARE) - 1)] if scores else HERO_SCORE)
    i = 0
    while i < len(work):
        slot = work[i]
        d = slot.end - slot.start
        fits = [s for s in pool if s.length >= d - 1e-3]
        if not fits:
            if slot.beats > 1:  # розбити на два коротші слоти
                mid_beat = slot.first_beat + slot.beats // 2
                frac = (slot.beats // 2) / slot.beats
                t_mid = slot.start + d * frac
                work[i:i + 1] = [Slot(slot.first_beat, mid_beat, slot.start, t_mid, slot.level, slot.accent),
                                 Slot(mid_beat, slot.last_beat, t_mid, slot.end, slot.level, slot.accent)]
                continue
            fits = sorted(pool, key=lambda s: -s.length)[:1]  # крайній випадок: найдовший доступний
            if not fits:
                raise ValueError("Немає жодного придатного шматка відео")
        total_used = sum(cams.values()) or 1.0
        share = {k: v / total_used for k, v in cams.items()}
        pos = (slot.start + d / 2) / total if total else 0
        best = max(fits, key=lambda s: value(s, slot, prev, pos, share, n_cams, w))

        # «герой» тримає кадр до 2 тактів, якщо шматок довгий і герой був не щойно
        hero = False
        if best.score >= hero_floor and slot.first_beat - last_hero_beat >= HERO_COOLDOWN:
            j = i + 1
            while (j < len(work) and work[j].last_beat - slot.first_beat <= HERO_MAX_BEATS
                   and work[j].end - slot.start <= best.length + 1e-3):
                j += 1
            if j > i + 1:
                merged = Slot(slot.first_beat, work[j - 1].last_beat, slot.start, work[j - 1].end,
                              slot.level, slot.accent or any(s.accent for s in work[i + 1:j]))
                work[i:j] = [merged]
                slot, d, hero, last_hero_beat = merged, merged.end - merged.start, True, slot.first_beat

        src_in = best.start + max(0.0, (best.length - d) / 2)
        out.append(Choice(slot, best, round(src_in, 4), hero=hero, reused=best.used > 0))
        best.used += 1
        cams[best.camera] = cams.get(best.camera, 0) + d
        prev = best
        i += 1
    return out
