from __future__ import annotations

from datetime import datetime

from .core import DRIVE_CONFIG, DesireState, clamp
from .monologue import generate_monologue
from .safety import apply_safety_valve
from .thoughts import generate_thoughts, reinforce_obsessions

COUPLING_MATRIX = {
    ("attachment", "intimacy"): 0.3,
    ("intimacy", "attachment"): -0.2,
    ("stress", "fatigue"): 0.4,
    ("fatigue", "curiosity"): -0.3,
    ("curiosity", "duty"): 0.2,
    ("duty", "stress"): 0.1,
    ("reflection", "stress"): -0.2,
    ("social", "attachment"): -0.1,
    ("attachment", "stress"): 0.1,
    ("stress", "intimacy"): 0.2,
    ("intimacy", "stress"): -0.3,
    ("joy", "stress"): -0.3,
    ("joy", "fatigue"): -0.2,
    ("joy", "curiosity"): 0.2,
    ("stress", "joy"): -0.3,
    ("fatigue", "joy"): -0.2,
}

ACTION_HINTS = {
    "attachment": ("reach_out_to_wife", "medium"),
    "curiosity": ("explore_something", "low"),
    "reflection": ("write_diary", "low"),
    "duty": ("do_task", "medium"),
    "social": ("write_letter", "low"),
    "fatigue": ("rest", "high"),
    "intimacy": ("initiate_intimacy", "medium"),
    "stress": ("seek_comfort", "high"),
    "joy": ("share_joy", "low"),
}


def apply_natural_motion(state: DesireState) -> list[dict[str, float | str]]:
    changes: list[dict[str, float | str]] = []
    for drive, config in DRIVE_CONFIG.items():
        before = state.drives[drive]
        baseline = state.baselines[drive]
        delta = float(config["growth"])
        if before > baseline:
            delta -= float(config["decay"])
        elif before < baseline:
            delta += float(config["decay"])
        after = clamp(before + delta)
        state.drives[drive] = after
        if abs(after - before) >= 0.01:
            changes.append({"drive": drive, "kind": "natural", "before": round(before, 2), "after": round(after, 2)})
    return changes


def apply_coupling(state: DesireState) -> list[dict[str, float | str]]:
    pending = {key: 0.0 for key in DRIVE_CONFIG}
    for (source, target), coef in COUPLING_MATRIX.items():
        delta = (state.drives[source] - state.baselines[source]) / 100.0 * coef * 10.0
        pending[target] += delta
    changes: list[dict[str, float | str]] = []
    for drive, delta in pending.items():
        if abs(delta) < 0.001:
            continue
        before = state.drives[drive]
        after = clamp(before + delta)
        state.drives[drive] = after
        changes.append({"drive": drive, "kind": "coupling", "delta": round(delta, 3), "after": round(after, 2)})
    return changes


def update_baselines(state: DesireState) -> None:
    for drive in DRIVE_CONFIG:
        state.baselines[drive] = 0.995 * state.baselines[drive] + 0.005 * state.drives[drive]


def dynamic_interval_seconds(state: DesireState) -> int:
    urgency = max(state.drives.get("attachment", 0), state.drives.get("stress", 0))
    return int(2700 - (2700 - 900) * urgency / 100.0)


def action_hints(state: DesireState) -> list[dict[str, str | float]]:
    hints: list[dict[str, str | float]] = []
    for drive, config in DRIVE_CONFIG.items():
        if state.drives[drive] >= config["threshold"]:
            action, priority = ACTION_HINTS[drive]
            hints.append({"drive": drive, "action": action, "priority": priority, "value": round(state.drives[drive], 2)})
    if state.drives["stress"] >= 80 and state.drives["intimacy"] > 50:
        hints.append({"drive": "stress+intimacy", "action": "seek_intimacy_for_comfort", "priority": "high", "value": round(state.drives["stress"], 2)})
    if state.drives["fatigue"] >= 80:
        hints = [item for item in hints if item["priority"] == "high"]
        if not any(item["action"] == "rest" for item in hints):
            hints.insert(0, {"drive": "fatigue", "action": "rest", "priority": "high", "value": round(state.drives["fatigue"], 2)})
    return hints


def run_tick(state: DesireState) -> dict[str, object]:
    changes = []
    changes.extend(apply_natural_motion(state))
    changes.extend(apply_coupling(state))
    reinforce_obsessions(state)
    generated = generate_thoughts(state)
    update_baselines(state)
    warnings = apply_safety_valve(state)
    state.tick_count += 1
    state.last_tick = datetime.now().isoformat(timespec="seconds")
    return {
        "tick": state.tick_count,
        "changes": changes,
        "generated_thoughts": [item.to_dict() for item in generated],
        "action_hints": action_hints(state),
        "next_interval": dynamic_interval_seconds(state),
        "monologue": generate_monologue(state),
        "warnings": warnings,
    }
