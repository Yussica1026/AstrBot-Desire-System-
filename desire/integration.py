from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from .core import DesireState, apply_event
from .thoughts import resolve_thought
from .tick import run_tick


class DesireEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self.connect() as conn:
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

    def load_state(self) -> DesireState:
        with self.connect() as conn:
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
        with self.connect() as own_conn:
            self.save_state(state, own_conn)

    def trigger_event(self, event_type: str) -> dict[str, Any]:
        state = self.load_state()
        changes = apply_event(state, event_type)
        self.save_state(state)
        return {"event": event_type, "changes": changes, "state": self.summary_from_state(state)}

    def tick(self) -> dict[str, Any]:
        state = self.load_state()
        result = run_tick(state)
        self.save_state(state)
        return result

    def resolve(self, thought_text: str) -> dict[str, Any]:
        state = self.load_state()
        ok = resolve_thought(state, thought_text)
        self.save_state(state)
        return {"resolved": ok, "state": self.summary_from_state(state)}

    def summary(self) -> dict[str, Any]:
        return self.summary_from_state(self.load_state())

    def summary_from_state(self, state: DesireState) -> dict[str, Any]:
        return {
            "drives": {key: round(value, 2) for key, value in state.drives.items()},
            "baselines": {key: round(value, 2) for key, value in state.baselines.items()},
            "thoughts": [item.to_dict() for item in state.thoughts],
            "tick_count": state.tick_count,
            "last_tick": state.last_tick,
        }
