"""Instance-scoped MCP adapter for the current desire models.

AstrBot's module-level integration API keeps its own database and behavior.
This adapter never redirects that API or changes an existing database schema.
"""

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta

from .core import DesireState, Drive, Thought, apply_event, create_default_drives
from .integration import TZ_MSK, _drives_to_json, _json_to_drives, _thoughts_to_json
from .monologue import generate_monologue
from .safety import safety_check
from .thoughts import decay_thoughts, maybe_spawn_thought, resolve_thought, sample_and_update
from .tick import calculate_tick_interval, tick


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    # Legacy MCP used datetime.now(): offset-less values describe the host's
    # local clock, whereas the current integration already stores an offset.
    return parsed.astimezone(TZ_MSK) if parsed.tzinfo is None else parsed


class DesireEngine:
    def __init__(self, db_path):
        self.db_path = os.fspath(db_path)
        if self.db_path == ":memory:":
            raise ValueError("Use a SQLite file path for persistent MCP state")
        self._lock = threading.RLock()
        self._resolved_sources = set()
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with closing(self.connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS desire_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    drives_json TEXT NOT NULL,
                    thoughts_json TEXT NOT NULL,
                    last_tick TEXT,
                    tick_count INTEGER DEFAULT 0
                )
            """)
            self._columns = {row["name"] for row in conn.execute("PRAGMA table_info(desire_state)")}
            self._legacy = "baselines_json" in self._columns
            exists = conn.execute("SELECT 1 FROM desire_state WHERE id = 1").fetchone()
            conn.commit()
        if not exists:
            state = DesireState(last_tick=datetime.now(TZ_MSK).isoformat())
            self.save_state(state)

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def load_state(self):
        with self._lock, closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM desire_state WHERE id = 1").fetchone()
            if row is None:
                return DesireState(last_tick=datetime.now(TZ_MSK).isoformat())
            raw_drives = json.loads(row["drives_json"])
            if all(isinstance(value, dict) for value in raw_drives.values()):
                drives = _json_to_drives(row["drives_json"])
            else:
                # Older MCP releases used numeric drives plus a separate baseline map.
                baselines = json.loads(row["baselines_json"]) if self._legacy else {}
                drives = create_default_drives()
                for name, value in raw_drives.items():
                    drive = drives.setdefault(name, Drive(name=name))
                    drive.value = float(value)
                    drive.baseline = float(baselines.get(name, drive.baseline))
            thoughts = []
            for index, item in enumerate(json.loads(row["thoughts_json"])):
                source = item.get("source_drive", item.get("source", ""))
                identifier = item.get("id") or hashlib.sha256(
                    f"{index}:{source}:{item['content']}".encode("utf-8")
                ).hexdigest()[:16]
                dates = {}
                for field in ("created_at", "last_hit"):
                    try:
                        dates[field] = _timestamp(item.get(field) or "").isoformat()
                    except (TypeError, ValueError):
                        dates[field] = datetime.now(TZ_MSK).isoformat()
                thoughts.append(Thought(
                    id=identifier, content=item["content"], source_drive=source,
                    weight=item.get("weight", 1.0),
                    hit_count=item.get("hit_count", item.get("count", 1)),
                    is_obsession=item.get("is_obsession", item.get("obsession", False)),
                    resolved=item.get("resolved", False), **dates,
                ))
            self._resolved_sources = (
                set(json.loads(row["resolved_sources_json"] or "[]"))
                if "resolved_sources_json" in self._columns else set()
            )
            return DesireState(drives=drives, thoughts=thoughts,
                               last_tick=row["last_tick"] or "", tick_count=row["tick_count"] or 0)

    def save_state(self, state):
        with self._lock, closing(self.connect()) as conn:
            if self._legacy:
                drives = json.dumps({name: drive.value for name, drive in state.drives.items()})
                thoughts = json.dumps([
                    {"content": t.content, "source": t.source_drive, "count": t.hit_count,
                     "obsession": t.is_obsession, "created_at": t.created_at, "last_hit": t.last_hit}
                    for t in state.thoughts if not t.resolved
                ], ensure_ascii=False)
            else:
                drives, thoughts = _drives_to_json(state.drives), _thoughts_to_json(state.thoughts)
            values = {"drives_json": drives, "thoughts_json": thoughts,
                      "last_tick": state.last_tick, "tick_count": state.tick_count}
            if self._legacy:
                values["baselines_json"] = json.dumps({name: d.baseline for name, d in state.drives.items()})
            if "resolved_sources_json" in self._columns:
                values["resolved_sources_json"] = json.dumps(sorted(self._resolved_sources))
            # UPDATE preserves unrelated columns and avoids REPLACE's delete/insert behavior.
            if conn.execute("SELECT 1 FROM desire_state WHERE id = 1").fetchone():
                assignments = ", ".join(name + " = ?" for name in values)
                conn.execute("UPDATE desire_state SET " + assignments + " WHERE id = 1", list(values.values()))
            else:
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                conn.execute(f"INSERT INTO desire_state (id, {columns}) VALUES (1, {placeholders})", list(values.values()))
            conn.commit()

    def _advance(self, state):
        result = tick(state)
        decay_thoughts(state)
        new_thought = maybe_spawn_thought(state)
        if new_thought:
            state.thoughts.append(new_thought)
        sampled = sample_and_update(state)
        warnings = safety_check(state)
        return {
            **result,
            "drive_changes": result["changes"],
            "new_thought": new_thought.content if new_thought else None,
            "sampled_thought": sampled.content if sampled else None,
            "monologue": generate_monologue(state, sampled),
            "warnings": warnings,
            "drives_snapshot": {name: round(d.value, 1) for name, d in state.drives.items()},
        }

    def trigger_event(self, event_type):
        with self._lock:
            state = self.load_state()
            changes = apply_event(state, event_type)
            self.save_state(state)
            return {"event": event_type, "changes": changes, "state": self.summary_from_state(state)}

    def tick(self):
        with self._lock:
            state = self.load_state()
            result = self._advance(state)
            self.save_state(state)
            return result

    def tick_if_due(self, max_ticks=96):
        with self._lock:
            state = self.load_state()
            now = datetime.now(TZ_MSK)
            try:
                last_tick = _timestamp(state.last_tick)
            except (TypeError, ValueError):
                last_tick = now
                state.last_tick = now.isoformat()
                self.save_state(state)
            count, latest = 0, None
            while count < max_ticks:
                interval = calculate_tick_interval(state)
                if (now - last_tick).total_seconds() < interval:
                    break
                latest = self._advance(state)
                last_tick += timedelta(seconds=interval)
                state.last_tick = last_tick.isoformat()
                count += 1
            if count:
                self.save_state(state)
            return {
                "ticks_run": count, "latest": latest,
                "remaining_backlog": (now - last_tick).total_seconds() >= calculate_tick_interval(state),
            }

    def seconds_until_due(self):
        with self._lock:
            state = self.load_state()
            try:
                elapsed = (datetime.now(TZ_MSK) - _timestamp(state.last_tick)).total_seconds()
            except (TypeError, ValueError):
                elapsed = 0.0
            return max(1.0, calculate_tick_interval(state) - elapsed)

    def resolve(self, thought_text):
        with self._lock:
            state = self.load_state()
            thought = resolve_thought(state, thought_text) if thought_text else None
            if thought:
                self._resolved_sources.add(thought.source_drive)
            self.save_state(state)
            return {"resolved": thought is not None, "state": self.summary_from_state(state)}

    def summary(self, catch_up=True):
        with self._lock:
            if catch_up:
                self.tick_if_due()
            return self.summary_from_state(self.load_state())

    def summary_from_state(self, state):
        return {
            "drives": {name: round(d.value, 2) for name, d in state.drives.items()},
            "baselines": {name: round(d.baseline, 2) for name, d in state.drives.items()},
            "thoughts": json.loads(_thoughts_to_json(state.thoughts)),
            "resolved_sources": sorted(self._resolved_sources),
            "tick_count": state.tick_count, "last_tick": state.last_tick,
        }
