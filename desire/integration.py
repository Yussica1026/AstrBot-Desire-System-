from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta
from typing import Any

from .core import DesireState, apply_event
from .thoughts import resolve_thought
from .tick import dynamic_interval_seconds, run_tick


class DesireEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with closing(self.connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS desire_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    drives_json TEXT NOT NULL,
                    baselines_json TEXT NOT NULL,
                    thoughts_json TEXT NOT NULL,
                    tick_count INTEGER NOT NULL DEFAULT 0,
                    last_tick TEXT NOT NULL
                )
                """
            )
            if not conn.execute("SELECT 1 FROM desire_state WHERE id = 1").fetchone():
                state = DesireState()
                self.save_state(state, conn)
            conn.commit()

    def load_state(self) -> DesireState:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM desire_state WHERE id = 1").fetchone()
        if not row:
            return DesireState()
        return DesireState.from_dict(
            {
                "drives": json.loads(row["drives_json"]),
                "baselines": json.loads(row["baselines_json"]),
                "thoughts": json.loads(row["thoughts_json"]),
                "tick_count": row["tick_count"],
                "last_tick": row["last_tick"],
            }
        )

    def save_state(self, state: DesireState, conn: sqlite3.Connection | None = None) -> None:
        payload = (
            1,
            json.dumps(state.drives, ensure_ascii=False),
            json.dumps(state.baselines, ensure_ascii=False),
            json.dumps([item.to_dict() for item in state.thoughts], ensure_ascii=False),
            state.tick_count,
            state.last_tick,
        )
        if conn is not None:
            conn.execute(
                """
                INSERT OR REPLACE INTO desire_state
                (id, drives_json, baselines_json, thoughts_json, tick_count, last_tick)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            return
        with closing(self.connect()) as own_conn:
            self.save_state(state, own_conn)
            own_conn.commit()

    def trigger_event(self, event_type: str) -> dict[str, Any]:
        with self._lock:
            state = self.load_state()
            changes = apply_event(state, event_type)
            self.save_state(state)
            return {"event": event_type, "changes": changes, "state": self.summary_from_state(state)}

    def tick(self) -> dict[str, Any]:
        with self._lock:
            state = self.load_state()
            result = run_tick(state)
            self.save_state(state)
            return result

    def tick_if_due(self, max_ticks: int = 96) -> dict[str, Any]:
        """Run elapsed heartbeats and return the latest result.

        Catch-up keeps MCP clients evolving even when their server is not kept
        alive. The cap prevents a long offline period from blocking startup.
        """
        with self._lock:
            state = self.load_state()
            now = datetime.now()
            try:
                last_tick = datetime.fromisoformat(state.last_tick)
            except (TypeError, ValueError):
                last_tick = now

            results: list[dict[str, Any]] = []
            while len(results) < max_ticks:
                interval = dynamic_interval_seconds(state)
                if (now - last_tick).total_seconds() < interval:
                    break
                result = run_tick(state)
                last_tick += timedelta(seconds=interval)
                state.last_tick = last_tick.isoformat(timespec="seconds")
                results.append(result)

            if results:
                self.save_state(state)
            return {
                "ticks_run": len(results),
                "latest": results[-1] if results else None,
                "remaining_backlog": len(results) == max_ticks,
            }

    def seconds_until_due(self) -> float:
        with self._lock:
            state = self.load_state()
            try:
                elapsed = (datetime.now() - datetime.fromisoformat(state.last_tick)).total_seconds()
            except (TypeError, ValueError):
                elapsed = 0.0
            return max(1.0, dynamic_interval_seconds(state) - elapsed)

    def resolve(self, thought_text: str) -> dict[str, Any]:
        with self._lock:
            state = self.load_state()
            ok = resolve_thought(state, thought_text)
            self.save_state(state)
            return {"resolved": ok, "state": self.summary_from_state(state)}

    def summary(self, catch_up: bool = True) -> dict[str, Any]:
        if catch_up:
            self.tick_if_due()
        with self._lock:
            return self.summary_from_state(self.load_state())

    def summary_from_state(self, state: DesireState) -> dict[str, Any]:
        return {
            "drives": {key: round(value, 2) for key, value in state.drives.items()},
            "baselines": {key: round(value, 2) for key, value in state.baselines.items()},
            "thoughts": [item.to_dict() for item in state.thoughts],
            "tick_count": state.tick_count,
            "last_tick": state.last_tick,
        }
