from __future__ import annotations

from .core import DesireState


def generate_monologue(state: DesireState) -> str:
    obsessions = [item for item in state.thoughts if item.obsession]
    if obsessions:
        obsessions.sort(key=lambda item: (item.count, item.last_hit), reverse=True)
        return obsessions[0].content
    if state.thoughts:
        state.thoughts.sort(key=lambda item: item.last_hit, reverse=True)
        return state.thoughts[0].content
    strongest = max(state.drives.items(), key=lambda item: item[1])
    return f"现在最明显的是 {strongest[0]}，数值 {strongest[1]:.1f}。"
