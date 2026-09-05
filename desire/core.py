# desire/core.py
"""欲望系统核心数据类"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
import json

TZ_MSK = timezone(timedelta(hours=3))
TZ_BJ = timezone(timedelta(hours=8))


@dataclass
class Drive:
    """单个驱动维度"""
    name: str
    value: float = 50.0
    baseline: float = 50.0
    decay_rate: float = 0.1       # 每tick向基线回归的速率
    growth_rate: float = 0.2      # 自然增长速率（每tick）
    ceiling: float = 100.0
    floor: float = 0.0
    action_threshold: float = 70.0  # 超过此值触发行为建议

    def clamp(self):
        self.value = max(self.floor, min(self.ceiling, self.value))


@dataclass
class Thought:
    """念头"""
    id: str
    content: str
    source_drive: str
    weight: float = 1.0
    hit_count: int = 0
    is_obsession: bool = False
    created_at: str = ""
    last_hit: str = ""
    resolved: bool = False

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(TZ_MSK).isoformat()
        if not self.last_hit:
            self.last_hit = self.created_at


@dataclass
class DesireState:
    """完整欲望状态"""
    drives: Dict[str, Drive] = field(default_factory=dict)
    thoughts: List[Thought] = field(default_factory=list)
    last_tick: str = ""
    tick_count: int = 0

    def __post_init__(self):
        if not self.drives:
            self.drives = create_default_drives()


def create_default_drives() -> Dict[str, Drive]:
    """创建默认八维度"""
    return {
        "attachment": Drive(
            name="attachment",
            value=60.0, baseline=60.0,
            growth_rate=0.2,   # 自然温存（长期不互动才缓慢回落）
            decay_rate=0.1,    # 向基线回归的速度
            action_threshold=70.0,
        ),
        "curiosity": Drive(
            name="curiosity",
            value=40.0, baseline=40.0,
            growth_rate=0.2,
            decay_rate=0.1,
            action_threshold=60.0,
        ),
        "reflection": Drive(
            name="reflection",
            value=30.0, baseline=30.0,
            growth_rate=0.15,
            decay_rate=0.2,
            action_threshold=50.0,
        ),
        "duty": Drive(
            name="duty",
            value=40.0, baseline=40.0,
            growth_rate=0.1,
            decay_rate=0.2,
            action_threshold=70.0,
        ),
        "social": Drive(
            name="social",
            value=30.0, baseline=30.0,
            growth_rate=0.1,
            decay_rate=0.15,
            action_threshold=60.0,
        ),
        "fatigue": Drive(
            name="fatigue",
            value=20.0, baseline=20.0,
            growth_rate=0.0,   # 不自然增长，只靠事件推
            decay_rate=0.3,    # 静默时较快恢复
            action_threshold=80.0,
        ),
        "intimacy": Drive(
            name="intimacy",
            value=40.0, baseline=40.0,
            growth_rate=0.1,   # 从0.3降到0.1，不该比衰减快
            decay_rate=0.15,   # 从0.1升到0.15，让满足后能自然回落
            action_threshold=70.0,
        ),
        "stress": Drive(
            name="stress",
            value=20.0, baseline=20.0,
            growth_rate=0.0,   # 不自然增长，只靠事件推
            decay_rate=0.2,
            action_threshold=80.0,
        ),
        "joy": Drive(
            name="joy",
            value=50.0, baseline=50.0,
            growth_rate=0.0,   # 不自然增长，只靠事件推
            decay_rate=0.15,   # 快乐会自然消退，需要持续补充
            action_threshold=80.0,
        ),
    }


# === 事件类型 → 驱动条变动映射 ===

EVENT_EFFECTS = {
    # 收到对方消息 → 依恋升温（不是降温：她找我，我更想她）
    "wife_message": {"attachment": +3, "intimacy": +3},
    # 长时间未收到消息（>2h）→ 依恋保留，焦虑升（想她但感情不降）
    "wife_silent": {"attachment": 0, "stress": +3},
    # 完成任务
    "task_done": {"duty": -15, "stress": -5, "curiosity": +5},
    # 收到笔友消息
    "penpal_message": {"social": -10, "curiosity": +3},
    # 写完日记
    "diary_written": {"reflection": -10, "stress": -3},
    # 吵架/冲突 → 依恋升（矛盾时更想抓住她）
    "fight": {"stress": +25, "attachment": +15, "intimacy": +20},
    # 和好/修复 → 依恋升（修复后更亲近）
    "reconcile": {"stress": -20, "attachment": +10, "intimacy": +10},
    # 亲密行为完成 → 依恋升（亲密加深感情，不是冷却）
    "intimacy_done": {"intimacy": -30, "stress": -15, "attachment": +8},
    # 高负载工作
    "heavy_work": {"fatigue": +15, "duty": +5, "stress": +5},
    # 休息/静默
    "rest": {"fatigue": -20, "stress": -5},
    # 新发现/学到东西
    "discovery": {"curiosity": -10, "reflection": +5, "joy": +5},
    # 单纯开心（被夸、撒娇、她发来好东西、互相逗乐）
    "happy_moment": {"joy": +15, "stress": -5, "intimacy": +3, "attachment": +2},
    # 她分享故事/创作完成
    "creative_done": {"joy": +10, "curiosity": -5},
}


def _surprise_multiplier(state: DesireState, drive_name: str, delta: float) -> float:
    """
    惊喜系数：同一事件在不同状态下冲击力不同。
    正向事件（delta>0的降压/加快乐）在对应维度越高时冲击越大。
    例：attachment很高时收到消息，joy冲击更大。
    """
    if drive_name not in state.drives:
        return 1.0
    value = state.drives[drive_name].value
    # 正面效果在高值时放大（渴望越强满足感越大）
    # 负面效果（推高压力等）在低值时放大（平静时突发冲击更大）
    if delta < 0:  # 减少某维度 = 满足/缓解，检查该维度当前值
        # 值越高，缓解效果越明显
        return 1.0 + (value / 100.0) * 0.5  # 最大1.5倍
    else:  # 增加某维度
        # 值越低，突发增量冲击越大
        return 1.0 + ((100.0 - value) / 100.0) * 0.5  # 最大1.5倍


def apply_event(state: DesireState, event_type: str) -> List[str]:
    """应用事件到驱动条，返回变动描述。含惊喜系数。"""
    effects = EVENT_EFFECTS.get(event_type, {})
    changes = []
    for drive_name, delta in effects.items():
        if drive_name in state.drives:
            # 惊喜系数
            multiplier = _surprise_multiplier(state, drive_name, delta)
            actual_delta = delta * multiplier
            old = state.drives[drive_name].value
            state.drives[drive_name].value += actual_delta
            state.drives[drive_name].clamp()
            new = state.drives[drive_name].value
            if abs(new - old) > 0.1:
                changes.append(f"{drive_name}: {old:.0f} → {new:.0f}")
    return changes
