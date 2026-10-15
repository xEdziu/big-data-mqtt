"""Układ kopalni (wspólny z panelem webowym): trasy maszyn i strefy bez zasięgu."""

import json
import math
from dataclasses import dataclass
from pathlib import Path

Point = tuple[float, float]


class Route:
    """Polilinia przejeżdżana tam i z powrotem (wjazd do przodka i wyjazd)."""

    def __init__(self, points: list[Point]):
        self.points = [tuple(p) for p in points]
        self._cum = [0.0]
        for a, b in zip(self.points, self.points[1:]):
            self._cum.append(self._cum[-1] + math.dist(a, b))
        self.length = self._cum[-1]

    def position_at(self, distance: float) -> Point:
        if self.length == 0:
            return self.points[0]
        s = distance % (2 * self.length)
        if s > self.length:
            s = 2 * self.length - s
        for i in range(1, len(self._cum)):
            if s <= self._cum[i]:
                (x1, y1), (x2, y2) = self.points[i - 1], self.points[i]
                seg = self._cum[i] - self._cum[i - 1]
                t = (s - self._cum[i - 1]) / seg if seg else 0.0
                return x1 + t * (x2 - x1), y1 + t * (y2 - y1)
        return self.points[-1]


def point_in_polygon(p: Point, polygon: list[Point]) -> bool:
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


@dataclass
class MineLayout:
    routes: dict[str, list[Route]]
    stations: dict[str, list[Point]]
    zones: list[tuple[str, list[Point]]]

    @classmethod
    def load(cls, path: Path | str) -> "MineLayout":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            routes={t: [Route(r) for r in rs] for t, rs in raw["routes"].items()},
            stations={t: [tuple(p) for p in ps] for t, ps in raw["stations"].items()},
            zones=[(z["name"], [tuple(p) for p in z["polygon"]]) for z in raw["zones"]],
        )

    def zone_at(self, p: Point) -> str | None:
        for name, polygon in self.zones:
            if point_in_polygon(p, polygon):
                return name
        return None
