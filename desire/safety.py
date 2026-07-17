from __future__ import annotations

from .core import DRIVE_CONFIG, DesireState, clamp


def apply_safety_valve(state: DesireState) -> list[str]:
    warnings: list[str] = []
    for key in DRIVE_CONFIG:
        before = state.drives.get(key, 0)
        state.drives[key] = clamp(before)
        state.baselines[key] = clamp(state.baselines.get(key, DRIVE_CONFIG[key]["baseline"]))
        if abs(state.baselines[key] - DRIVE_CONFIG[key]["baseline"]) > 30:
            warnings.append(f"{key} baseline drift exceeds 30")
    if state.drives.get("fatigue", 0) >= 80:
        warnings.append("fatigue gate active: high priority rest only")
    return warnings
