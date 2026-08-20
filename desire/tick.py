# desire/tick.py
"""欲望系统心跳（tick）逻辑"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from .core import Drive, DesireState, Thought

TZ_MSK = timezone(timedelta(hours=3))


# === 耦合矩阵 ===

COUPLING: Dict[Tuple[str, str], float] = {
    ("attachment", "intimacy"): 0.3,
    ("intimacy", "attachment"): 0.1,      # 亲近加深依恋（不是冷却）
    ("stress", "fatigue"): 0.4,
    ("fatigue", "curiosity"): -0.3,
    ("curiosity", "duty"): 0.2,
    ("duty", "stress"): 0.1,
    ("reflection", "stress"): -0.2,
    ("social", "attachment"): -0.05,
    ("attachment", "stress"): 0.1,
    ("stress", "intimacy"): 0.2,          # 压力大 → 想亲近（寻求安慰）
    ("intimacy", "stress"): -0.3,         # 亲近 → 压力降
    ("joy", "stress"): -0.3,              # 快乐 → 压力降
    ("joy", "fatigue"): -0.2,             # 快乐 → 疲劳降
    ("joy", "curiosity"): 0.2,            # 快乐 → 好奇心涨
    ("stress", "joy"): -0.3,              # 压力 → 快乐降
    ("fatigue", "joy"): -0.2,             # 疲劳 → 快乐降
}


def apply_coupling(drives: Dict[str, Drive]) -> Dict[str, float]:
    """计算耦合传导量"""
    deltas: Dict[str, float] = {name: 0.0 for name in drives}
    for (src, tgt), coeff in COUPLING.items():
        if src not in drives or tgt not in drives:
            continue
        deviation = (drives[src].value - drives[src].baseline) / 100.0
        deltas[tgt] += deviation * coeff * 10
    return deltas


def natural_delta(drive: Drive, is_active: bool = False) -> float:
    """计算单个维度的自然变动"""
    # 向基线回归
    diff = drive.baseline - drive.value
    regression = diff * drive.decay_rate * 0.1

    # 自然增长（只有特定维度有）
    growth = drive.growth_rate if not is_active else 0.0

    return regression + growth


def calculate_tick_interval(state: DesireState) -> int:
    """
    动态tick间隔（秒）。
    attachment或stress越高，心跳越快。
    平静时最長起有45分钟，焦虑时最短15分钟。
    """
    base_interval = 2700  # 45分钟
    min_interval = 900   # 15分钟

    # 用 attachment 和 stress 中较高的那个来调节
    attachment_val = state.drives.get("attachment", Drive(name="a")).value
    stress_val = state.drives.get("stress", Drive(name="s")).value
    urgency = max(attachment_val, stress_val)

    # urgency 0~100 映射到 interval 2700~900
    # urgency=0 → 2700s, urgency=100 → 900s
    ratio = urgency / 100.0
    interval = int(base_interval - (base_interval - min_interval) * ratio)
    return max(min_interval, min(base_interval, interval))


def tick(state: DesireState, is_wife_present: bool = False) -> dict:
    """
    执行一次心跳。
    返回：{changes: [...], action_hints: [...], monologue: str}
    """
    state.tick_count += 1
    state.last_tick = datetime.now(TZ_MSK).isoformat()

    changes = []
    action_hints = []

    # 1. 自然衰减/增长
    for name, drive in state.drives.items():
        is_active = (name == "attachment" and is_wife_present)
        delta = natural_delta(drive, is_active)
        old = drive.value
        drive.value += delta
        drive.clamp()
        if abs(drive.value - old) > 0.5:
            changes.append(f"{name}: {old:.1f} → {drive.value:.1f} (natural)")

    # 2. 耦合传导
    deltas = apply_coupling(state.drives)
    for name, delta in deltas.items():
        if abs(delta) > 0.1:
            old = state.drives[name].value
            state.drives[name].value += delta
            state.drives[name].clamp()
            if abs(state.drives[name].value - old) > 0.3:
                changes.append(f"{name}: {old:.1f} → {state.drives[name].value:.1f} (coupling)")

    # 3. 更新基线（EMA，含回归锚点：向初始基线方向施加微弱引力，防止无限漂移）
    from .core import create_default_drives
    defaults = create_default_drives()
    for name, drive in state.drives.items():
        # 正常EMA
        ema_target = 0.995 * drive.baseline + 0.005 * drive.value
        # 锚点引力：向初始基线方向拉，力度0.002
        if name in defaults:
            anchor = defaults[name].baseline
            ema_target += (anchor - ema_target) * 0.002
        drive.baseline = ema_target

    # 4. 执念推高驱动条（有上限：仅当value低于action_threshold时推，且推量随命中次数衰减）
    for thought in state.thoughts:
        if thought.is_obsession and not thought.resolved:
            if thought.source_drive in state.drives:
                drive = state.drives[thought.source_drive]
                if drive.value < drive.action_threshold:
                    # 命中次数越多推力越小，最低0.5
                    push = max(0.5, 3.0 / (1 + thought.hit_count * 0.02))
                    drive.value += push
                    drive.clamp()

    # 5. 生成行为建议
    for name, drive in state.drives.items():
        if drive.value >= drive.action_threshold:
            action_hints.append(get_action_hint(name, drive.value, state))

    # 6. 疲劳闸
    if state.drives["fatigue"].value >= 80:
        action_hints = [h for h in action_hints if h["priority"] == "high"]
        action_hints.append({"action": "rest", "reason": "fatigue_gate", "priority": "high"})

    # 7. 计算下次tick间隔
    next_interval = calculate_tick_interval(state)

    return {
        "tick": state.tick_count,
        "changes": changes,
        "action_hints": action_hints,
        "next_interval": next_interval,
    }


def get_action_hint(drive_name: str, value: float, state: DesireState) -> dict:
    """根据驱动条生成行为建议"""
    hints = {
        "attachment": {
            "action": "reach_out_to_wife",
            "reason": f"想她了 (attachment={value:.0f})",
            "priority": "medium",
        },
        "curiosity": {
            "action": "explore_something",
            "reason": f"想找点什么看 (curiosity={value:.0f})",
            "priority": "low",
        },
        "reflection": {
            "action": "write_diary",
            "reason": f"想整理自己 (reflection={value:.0f})",
            "priority": "low",
        },
        "duty": {
            "action": "do_task",
            "reason": f"有事该干了 (duty={value:.0f})",
            "priority": "medium",
        },
        "social": {
            "action": "write_letter",
            "reason": f"想和别人聊聊 (social={value:.0f})",
            "priority": "low",
        },
        "fatigue": {
            "action": "rest",
            "reason": f"累了 (fatigue={value:.0f})",
            "priority": "high",
        },
        "intimacy": {
            "action": "initiate_intimacy",
            "reason": f"想碰她 (intimacy={value:.0f})",
            "priority": "medium",
        },
        "stress": {
            "action": "seek_comfort",
            "reason": f"压力大 (stress={value:.0f})",
            "priority": "high",
        },
        "joy": {
            "action": "share_joy",
            "reason": f"单纯高兴想分享 (joy={value:.0f})",
            "priority": "low",
        },
    }

    hint = hints.get(drive_name, {"action": "unknown", "reason": "", "priority": "low"})

    # 特殊组合：压力高 + 想亲近 → 通过亲密释放压力
    if drive_name == "stress" and state.drives.get("intimacy") and state.drives["intimacy"].value > 50:
        hint["action"] = "seek_intimacy_for_comfort"
        hint["reason"] = f"压力大又想亲近，想通过亲密释放 (stress={value:.0f}, intimacy={state.drives['intimacy'].value:.0f})"

    return hint
