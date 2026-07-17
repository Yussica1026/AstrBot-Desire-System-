from __future__ import annotations

import random
from datetime import datetime, timedelta

from .core import DRIVE_CONFIG, DesireState, Thought, clamp

THOUGHT_TEMPLATES = {
    "attachment": ["想知道她现在在做什么。", "有点想听到她的声音。", "想靠近一点，但不想打扰她。"],
    "curiosity": ["想找点新的东西看看。", "也许可以试一个新想法。", "这个问题还可以继续挖。"],
    "reflection": ["需要把今天发生的事整理一下。", "有些念头应该写下来。", "想弄清楚自己为什么这样想。"],
    "duty": ["还有事情该处理。", "先把该做的做完。", "不能让任务一直悬着。"],
    "social": ["也许该给谁写几句话。", "想和外面有一点连接。", "可以听听别人的近况。"],
    "fatigue": ["有点累了，应该慢一点。", "现在不适合硬撑。", "需要休息一下。"],
    "intimacy": ["想要更近一点。", "想被安慰，也想安慰她。", "想确认彼此还在。"],
    "stress": ["压力有点上来了。", "需要找个出口。", "这件事不能一直压着。"],
    "joy": ["这点开心想保存一下。", "想把快乐分享出去。", "今天也不是只有沉重。"],
}


def parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now()


def is_suppressed(state: DesireState, source: str) -> bool:
    if source == "attachment" and state.drives.get("duty", 0) >= 80:
        return True
    if source == "social" and state.drives.get("fatigue", 0) >= 70:
        return True
    return False


def expire_thoughts(state: DesireState) -> None:
    now = datetime.now()
    kept: list[Thought] = []
    for thought in state.thoughts:
        age = now - parse_dt(thought.last_hit)
        limit = timedelta(hours=72 if thought.obsession else 24)
        if age <= limit:
            kept.append(thought)
    state.thoughts = kept


def add_or_hit_thought(state: DesireState, content: str, source: str) -> Thought:
    now = datetime.now().isoformat(timespec="seconds")
    for thought in state.thoughts:
        if thought.content == content:
            thought.count += 1
            thought.last_hit = now
            if thought.count >= 3:
                thought.obsession = True
            return thought
    thought = Thought(content=content, source=source, last_hit=now, created_at=now)
    state.thoughts.append(thought)
    return thought


def trim_obsessions(state: DesireState) -> None:
    obsessions = [item for item in state.thoughts if item.obsession]
    if len(obsessions) <= 3:
        return
    obsessions.sort(key=lambda item: (item.count, item.last_hit), reverse=True)
    allowed = {id(item) for item in obsessions[:3]}
    for thought in state.thoughts:
        if thought.obsession and id(thought) not in allowed:
            thought.obsession = False


def generate_thoughts(state: DesireState, rng: random.Random | None = None) -> list[Thought]:
    rng = rng or random
    expire_thoughts(state)
    generated: list[Thought] = []
    existing = {item.content for item in state.thoughts}
    for source in DRIVE_CONFIG:
        value = state.drives.get(source, 0)
        probability = max(0.0, (value - 30.0) / 100.0)
        if probability <= 0 or is_suppressed(state, source):
            continue
        if rng.random() > probability:
            continue
        choices = [item for item in THOUGHT_TEMPLATES[source] if item not in existing]
        if not choices:
            choices = THOUGHT_TEMPLATES[source]
        thought = add_or_hit_thought(state, rng.choice(choices), source)
        generated.append(thought)
        existing.add(thought.content)
    trim_obsessions(state)
    return generated


def reinforce_obsessions(state: DesireState) -> None:
    for thought in state.thoughts:
        if thought.obsession and thought.source in state.drives:
            state.drives[thought.source] = clamp(state.drives[thought.source] + 3)


def resolve_thought(state: DesireState, text: str) -> bool:
    needle = text.strip()
    for thought in list(state.thoughts):
        if thought.content == needle or needle in thought.content:
            state.thoughts.remove(thought)
            if thought.source == "reflection":
                state.drives["joy"] = clamp(state.drives.get("joy", 0) + 3)
            return True
    return False
