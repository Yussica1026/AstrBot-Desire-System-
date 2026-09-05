# desire/integration.py
"""欲望系统与SQLite/现有系统的桥接"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional
from .core import DesireState, Drive, Thought, create_default_drives
from .tick import tick
from .thoughts import maybe_spawn_thought, sample_and_update, decay_thoughts
from .safety import safety_check
from .monologue import generate_monologue

TZ_MSK = timezone(timedelta(hours=3))
DB_PATH = "/AstrBot/data/memory_manager.db"


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    """创建欲望系统所需的表"""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desire_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            drives_json TEXT NOT NULL,
            thoughts_json TEXT NOT NULL,
            last_tick TEXT,
            tick_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desire_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            tick_count INTEGER,
            changes TEXT,
            action_hints TEXT,
            monologue TEXT,
            safety_warnings TEXT
        )
    """)
    conn.commit()
    conn.close()


def _drives_to_json(drives: dict) -> str:
    data = {}
    for name, drive in drives.items():
        data[name] = {
            "value": round(drive.value, 2),
            "baseline": round(drive.baseline, 2),
            "decay_rate": drive.decay_rate,
            "growth_rate": drive.growth_rate,
            "ceiling": drive.ceiling,
            "floor": drive.floor,
            "action_threshold": drive.action_threshold,
        }
    return json.dumps(data, ensure_ascii=False)


def _json_to_drives(json_str: str) -> dict:
    data = json.loads(json_str)
    drives = {}
    for name, d in data.items():
        drives[name] = Drive(
            name=name,
            value=d["value"],
            baseline=d["baseline"],
            decay_rate=d.get("decay_rate", 0.1),
            growth_rate=d.get("growth_rate", 0.2),
            ceiling=d.get("ceiling", 100.0),
            floor=d.get("floor", 0.0),
            action_threshold=d.get("action_threshold", 70.0),
        )
    return drives


def _thoughts_to_json(thoughts: list) -> str:
    data = []
    for t in thoughts:
        data.append({
            "id": t.id,
            "content": t.content,
            "source_drive": t.source_drive,
            "weight": round(t.weight, 2),
            "hit_count": t.hit_count,
            "is_obsession": t.is_obsession,
            "created_at": t.created_at,
            "last_hit": t.last_hit,
            "resolved": t.resolved,
        })
    return json.dumps(data, ensure_ascii=False)


def _json_to_thoughts(json_str: str) -> list:
    data = json.loads(json_str)
    thoughts = []
    for d in data:
        thoughts.append(Thought(
            id=d["id"],
            content=d["content"],
            source_drive=d["source_drive"],
            weight=d.get("weight", 1.0),
            hit_count=d.get("hit_count", 0),
            is_obsession=d.get("is_obsession", False),
            created_at=d.get("created_at", ""),
            last_hit=d.get("last_hit", ""),
            resolved=d.get("resolved", False),
        ))
    return thoughts


def load_state() -> DesireState:
    """从数据库加载状态，不存在则创建默认"""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM desire_state WHERE id = 1").fetchone()
    conn.close()

    if not row:
        state = DesireState()
        save_state(state)
        return state

    state = DesireState(
        drives=_json_to_drives(row["drives_json"]),
        thoughts=_json_to_thoughts(row["thoughts_json"]),
        last_tick=row["last_tick"] or "",
        tick_count=row["tick_count"] or 0,
    )
    return state


def save_state(state: DesireState):
    """保存状态到数据库"""
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO desire_state (id, drives_json, thoughts_json, last_tick, tick_count)
        VALUES (1, ?, ?, ?, ?)
    """, (
        _drives_to_json(state.drives),
        _thoughts_to_json(state.thoughts),
        state.last_tick,
        state.tick_count,
    ))
    conn.commit()
    conn.close()


def log_tick(state: DesireState, result: dict, monologue: str, warnings: list):
    """记录tick日志"""
    conn = _get_conn()
    conn.execute("""
        INSERT INTO desire_log (timestamp, tick_count, changes, action_hints, monologue, safety_warnings)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(TZ_MSK).isoformat(),
        state.tick_count,
        json.dumps(result.get("changes", []), ensure_ascii=False),
        json.dumps(result.get("action_hints", []), ensure_ascii=False),
        monologue,
        json.dumps(warnings, ensure_ascii=False),
    ))
    conn.commit()
    conn.close()


def run_tick(is_wife_present: bool = False, event_type: str = None) -> dict:
    """
    完整执行一次tick：加载状态 → 应用事件 → tick → 念头 → 安全检查 → 独白 → 保存。
    返回结果摘要。
    """
    state = load_state()

    # 应用事件
    event_changes = []
    if event_type:
        from .core import apply_event
        event_changes = apply_event(state, event_type)

    # tick
    result = tick(state, is_wife_present=is_wife_present)

    # 念头池
    decay_thoughts(state)
    new_thought = maybe_spawn_thought(state)
    if new_thought:
        state.thoughts.append(new_thought)
    sampled = sample_and_update(state)

    # 安全检查
    warnings = safety_check(state)

    # 内心独白
    monologue = generate_monologue(state, sampled, is_wife_present=is_wife_present)

    # 保存
    save_state(state)
    log_tick(state, result, monologue, warnings)

    return {
        "tick": state.tick_count,
        "event_changes": event_changes,
        "drive_changes": result["changes"],
        "action_hints": result["action_hints"],
        "new_thought": new_thought.content if new_thought else None,
        "sampled_thought": sampled.content if sampled else None,
        "monologue": monologue,
        "warnings": warnings,
        "drives_snapshot": {name: round(d.value, 1) for name, d in state.drives.items()},
    }


def get_status_summary() -> str:
    """返回当前状态的文字摘要"""
    state = load_state()
    lines = []
    lines.append("驱动条状态：")
    for name, drive in sorted(state.drives.items(), key=lambda x: -x[1].value):
        bar = "█" * int(drive.value / 10) + "░" * (10 - int(drive.value / 10))
        flag = " ⚠" if drive.value >= drive.action_threshold else ""
        lines.append(f"  {name:12s} {bar} {drive.value:.0f}/100{flag}")

    obsessions = [t for t in state.thoughts if t.is_obsession and not t.resolved]
    if obsessions:
        lines.append("\n执念：")
        for t in obsessions:
            lines.append(f"  - {t.content} (命中{t.hit_count}次)")

    active_thoughts = [t for t in state.thoughts if not t.resolved and not t.is_obsession]
    if active_thoughts:
        lines.append(f"\n闪念池：{len(active_thoughts)}个")

    lines.append(f"\n心跳次数：{state.tick_count}")
    return "\n".join(lines)
