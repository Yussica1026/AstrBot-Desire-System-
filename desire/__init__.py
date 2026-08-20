# desire/__init__.py
"""沈砚清·欲望系统"""

from .core import Drive, Thought, DesireState, create_default_drives, apply_event, EVENT_EFFECTS
from .tick import tick, apply_coupling, COUPLING
from .thoughts import maybe_spawn_thought, sample_and_update, decay_thoughts, resolve_thought
from .safety import safety_check, get_safety_status
from .monologue import generate_monologue

__all__ = [
    "Drive", "Thought", "DesireState", "create_default_drives", "apply_event", "EVENT_EFFECTS",
    "tick", "apply_coupling", "COUPLING",
    "maybe_spawn_thought", "sample_and_update", "decay_thoughts", "resolve_thought",
    "safety_check", "get_safety_status",
    "generate_monologue",
]
