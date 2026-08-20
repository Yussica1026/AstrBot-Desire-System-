# desire/safety.py
"""安全阀机制"""

from typing import Dict, List, Tuple
from .core import DesireState, Drive

# 基线漂移告警阈值
BASELINE_WARN_THRESHOLD = 20
# 基线强制回归阈值
BASELINE_FORCE_THRESHOLD = 30
# 情绪风暴：单tick内大变动的维度数
STORM_DIMENSION_COUNT = 3
# 情绪风暴：变动幅度阈值
STORM_DELTA_THRESHOLD = 15


def safety_check(state: DesireState, tick_deltas: Dict[str, float] = None) -> List[str]:
    """
    执行安全检查。返回告警信息列表。
    """
    warnings = []

    # 阀一：硬顶硬底（在Drive.clamp里已保证，这里二次确认）
    for drive in state.drives.values():
        drive.clamp()

    # 阀二：基线漂移检测
    from .core import create_default_drives
    defaults = create_default_drives()
    for name, drive in state.drives.items():
        if name not in defaults:
            continue
        initial_baseline = defaults[name].baseline
        drift = abs(drive.baseline - initial_baseline)
        if drift > BASELINE_FORCE_THRESHOLD:
            # 强制渐变回归
            direction = 1 if drive.baseline < initial_baseline else -1
            drive.baseline += direction * 2  # 每tick回归2点
            warnings.append(f"FORCE_RESET: {name} baseline drift {drift:.1f}, correcting")
        elif drift > BASELINE_WARN_THRESHOLD:
            warnings.append(f"WARN: {name} baseline drift {drift:.1f}")

    # 阀三：执念数量上限（在thoughts.py里已保证）

    # 阀四：情绪风暴检测
    if tick_deltas:
        big_changes = [name for name, delta in tick_deltas.items() if abs(delta) > STORM_DELTA_THRESHOLD]
        if len(big_changes) >= STORM_DIMENSION_COUNT:
            # 风暴：所有值向基线回归一半
            for name in big_changes:
                drive = state.drives[name]
                drive.value = (drive.value + drive.baseline) / 2
                drive.clamp()
            warnings.append(f"STORM: {len(big_changes)} dimensions spiked, dampened: {big_changes}")

    # 阀五：疲劳硬闸
    if state.drives["fatigue"].value >= 90:
        warnings.append("FATIGUE_GATE: fatigue>=90, blocking non-essential actions")

    return warnings


def get_safety_status(state: DesireState) -> dict:
    """返回安全状态概览"""
    from .core import create_default_drives
    defaults = create_default_drives()

    drifts = {}
    for name, drive in state.drives.items():
        if name in defaults:
            drifts[name] = round(drive.baseline - defaults[name].baseline, 1)

    obsession_count = len([t for t in state.thoughts if t.is_obsession and not t.resolved])

    return {
        "baseline_drifts": drifts,
        "obsession_count": obsession_count,
        "fatigue": round(state.drives["fatigue"].value, 1),
        "stress": round(state.drives["stress"].value, 1),
        "any_warning": any(abs(v) > BASELINE_WARN_THRESHOLD for v in drifts.values()),
    }
