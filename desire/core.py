from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

DRIVE_CONFIG = {
    "attachment": {"growth": 0.5, "decay": 0.3, "threshold": 70, "baseline": 35},
    "curiosity": {"growth": 0.2, "decay": 0.1, "threshold": 60, "baseline": 45},
    "reflection": {"growth": 0.15, "decay": 0.2, "threshold": 50, "baseline": 40},
    "duty": {"growth": 0.1, "decay": 0.2, "threshold": 70, "baseline": 45},
    "social": {"growth": 0.1, "decay": 0.15, "threshold": 60, "baseline": 40},
    "fatigue": {"growth": 0.0, "decay": 0.3, "threshold": 80, "baseline": 25},
    "intimacy": {"growth": 0.3, "decay": 0.1, "threshold": 70, "baseline": 35},
    "stress": {"growth": 0.0, "decay": 0.2, "threshold": 80, "baseline": 25},
    "joy": {"growth": 0.0, "decay": 0.15, "threshold": 80, "baseline": 35},
}

EVENT_EFFECTS = {
    "wife_message": {"attachment": -5, "intimacy": 3},
    "wife_silent": {"attachment": 10, "stress": 3},
    "task_done": {"duty": -15, "stress": -5, "curiosity": 5},
    "penpal_message": {"social": -10, "curiosity": 3},
    "diary_written": {"reflection": -10, "stress": -3},
    "fight": {"stress": 25, "attachment": 15, "intimacy": 20},
    "reconcile": {"stress": -20, "attachment": -5, "intimacy": 10},
    "intimacy_done": {"intimacy": -30, "stress": -15, "attachment": -10},
    "heavy_work": {"fatigue": 15, "duty": 5, "stress": 5},
    "rest": {"fatigue": -20, "stress": -5},
    "discovery": {"curiosity": -10, "reflection": 5, "joy": 5},
    "happy_moment": {"joy": 15, "stress": -5, "intimacy": 3},
    "creative_done": {"joy": 10, "curiosity": -5},
}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def default_drives() -> dict[str, float]:
    return {key: float(cfg["baseline"]) for key, cfg in DRIVE_CONFIG.items()}


def default_baselines() -> dict[str, float]:
    return {key: float(cfg["baseline"]) for key, cfg in DRIVE_CONFIG.items()}


@dataclass
class Thought:
    content: str
    source: str
    count: int = 1
    obsession: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    last_hit: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Thought":
        return cls(
            content=str(data.get("content", "")),
            source=str(data.get("source", "")),
            count=int(data.get("count", 1)),
            obsession=bool(data.get("obsession", False)),
            created_at=str(data.get("created_at") or datetime.now().isoformat(timespec="seconds")),
            last_hit=str(data.get("last_hit") or datetime.now().isoformat(timespec="seconds")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "source": self.source,
            "count": self.count,
            "obsession": self.obsession,
            "created_at": self.created_at,
            "last_hit": self.last_hit,
        }


@dataclass
class DesireState:
    drives: dict[str, float] = field(default_factory=default_drives)
    baselines: dict[str, float] = field(default_factory=default_baselines)
    thoughts: list[Thought] = field(default_factory=list)
    tick_count: int = 0
    last_tick: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DesireState":
        drives = default_drives()
        drives.update({k: clamp(v) for k, v in dict(data.get("drives", {})).items() if k in DRIVE_CONFIG})
        baselines = default_baselines()
        baselines.update({k: clamp(v) for k, v in dict(data.get("baselines", {})).items() if k in DRIVE_CONFIG})
        thoughts = [Thought.from_dict(item) for item in data.get("thoughts", []) if isinstance(item, dict)]
        state = cls(
            drives=drives,
            baselines=baselines,
            thoughts=thoughts,
            tick_count=int(data.get("tick_count", 0)),
            last_tick=str(data.get("last_tick") or datetime.now().isoformat(timespec="seconds")),
        )
        state.clamp_all()
        return state

    def to_dict(self) -> dict[str, Any]:
        return {
            "drives": self.drives,
            "baselines": self.baselines,
            "thoughts": [item.to_dict() for item in self.thoughts],
            "tick_count": self.tick_count,
            "last_tick": self.last_tick,
        }

    def clamp_all(self) -> None:
        for key in DRIVE_CONFIG:
            self.drives[key] = clamp(self.drives.get(key, DRIVE_CONFIG[key]["baseline"]))
            self.baselines[key] = clamp(self.baselines.get(key, DRIVE_CONFIG[key]["baseline"]))


def surprise_multiplier(current_value: float, delta: float) -> float:
    current_value = clamp(current_value)
    if delta < 0:
        return 1.0 + (current_value / 100.0) * 0.5
    if delta > 0:
        return 1.0 + ((100.0 - current_value) / 100.0) * 0.5
    return 1.0


def apply_event(state: DesireState, event_type: str) -> list[dict[str, float | str]]:
    effects = EVENT_EFFECTS.get(event_type)
    if not effects:
        return []
    changes: list[dict[str, float | str]] = []
    for drive, base_delta in effects.items():
        before = state.drives[drive]
        multiplier = surprise_multiplier(before, base_delta)
        actual_delta = base_delta * multiplier
        after = clamp(before + actual_delta)
        state.drives[drive] = after
        changes.append(
            {
                "drive": drive,
                "before": round(before, 2),
                "base_delta": round(base_delta, 2),
                "multiplier": round(multiplier, 3),
                "actual_delta": round(actual_delta, 2),
                "after": round(after, 2),
            }
        )
    state.clamp_all()
    return changes
