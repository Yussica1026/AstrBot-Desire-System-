"""MCP regressions using synthetic data and retained, isolated SQLite files.

Only the local loopback HTTP server is contacted. Test artifacts are deliberately
kept; the retained directory is printed before unittest's final summary.
"""

from __future__ import annotations

from contextlib import closing
import http.client
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from desire.core import DesireState, Thought, TZ_MSK, create_default_drives
from desire.tick import calculate_tick_interval


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = {
    "desire_status",
    "desire_event",
    "desire_tick",
    "desire_resolve_thought",
}


class DesireMCPRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifact_root = Path(tempfile.mkdtemp(prefix="desire-mcp-regression-"))

    @classmethod
    def tearDownClass(cls):
        sys.stderr.write(f"\nRetained test artifacts: {cls.artifact_root}\n")

    def setUp(self):
        self.case_dir = self.artifact_root / self._testMethodName
        self.case_dir.mkdir()
        self.db_path = self.case_dir / "mcp.db"
        self.integration_sentinel = self.case_dir / "integration-must-not-open.db"
        self.integration = importlib.import_module("desire.integration")
        self.addCleanup(self._check_integration_sentinel)
        self._patch(patch.object(self.integration, "DB_PATH", str(self.integration_sentinel)))
        self._patch(patch.dict(os.environ, {"DESIRE_DB_FILE": str(self.db_path)}))
        self.engine_module = importlib.import_module("desire.mcp_engine")
        self.Engine = self.engine_module.DesireEngine
        self.mcp_module = importlib.import_module("mcp_server")
        self._patch(patch.object(self.mcp_module, "DB_FILE", str(self.db_path)))

        # Keep production thought logic, but supply only synthetic test text.
        thoughts = importlib.import_module("desire.thoughts")
        self._patch(patch.dict(
            thoughts.THOUGHT_TEMPLATES,
            {name: [f"Synthetic observation for {name}"] for name in thoughts.THOUGHT_TEMPLATES},
            clear=True,
        ))
        monologue = importlib.import_module("desire.monologue")
        self._patch(patch.dict(
            monologue.TEMPLATES,
            {name: ["Synthetic heartbeat observation."] for name in monologue.TEMPLATES},
            clear=True,
        ))

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def _check_integration_sentinel(self):
        self.assertFalse(self.integration_sentinel.exists(), "MCP touched integration.DB_PATH")

    def _seed(self):
        state = DesireState(last_tick=datetime.now(TZ_MSK).isoformat(), tick_count=12)
        state.drives["curiosity"].value = 64.5
        state.drives["curiosity"].baseline = 47.5
        state.thoughts = [Thought(
            id="synthetic-diagram",
            content="Review the sample diagram",
            source_drive="reflection",
            weight=0.8,
        )]
        return state

    def _assert_summary(self, summary):
        self.assertIsInstance(summary, dict)
        for key in ("drives", "baselines", "thoughts", "tick_count", "last_tick"):
            self.assertIn(key, summary)
        expected_names = set(create_default_drives())
        self.assertEqual(set(summary["drives"]), expected_names)
        self.assertEqual(set(summary["baselines"]), expected_names)
        for values in (summary["drives"], summary["baselines"]):
            for value in values.values():
                self.assertIsInstance(value, (int, float))
                self.assertNotIsInstance(value, bool)
        self.assertIsInstance(summary["thoughts"], list)
        self.assertIsInstance(summary["tick_count"], int)
        self.assertIsInstance(summary["last_tick"], str)

    def _rpc(self, server, method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        response = server.handle(json.loads(json.dumps(message)))
        self.assertIsInstance(response, dict)
        self.assertEqual(response.get("jsonrpc"), "2.0")
        self.assertEqual(response.get("id"), request_id)
        self.assertNotIn("error", response)
        return response["result"]

    def _call(self, server, name, arguments=None):
        result = self._rpc(server, "tools/call", {"name": name, "arguments": arguments or {}})
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["type"], "text")
        return json.loads(result["content"][0]["text"])

    def test_import_entrypoints_without_opening_a_database(self):
        script = (
            "import sqlite3, sys\n"
            "def refuse_connection(*args, **kwargs):\n"
            "    raise AssertionError('Import attempted to open a database')\n"
            "sqlite3.connect = refuse_connection\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import mcp_server\n"
            "import mcp_http_server\n"
            "print('MCP imports OK')\n"
        )
        child_env = os.environ.copy()
        child_env.pop("DESIRE_DB_FILE", None)
        child_env["PYTHONDONTWRITEBYTECODE"] = "1"
        completed = subprocess.run(
            [sys.executable, "-c", script, str(REPO_ROOT)],
            cwd=self.case_dir,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "MCP imports OK")
        self.assertFalse(list(self.case_dir.glob("*.db")))

    def test_all_four_jsonrpc_tools_persist_across_reopening(self):
        self.Engine(str(self.db_path)).save_state(self._seed())
        server = self.mcp_module.DesireMCPServer()
        initialized = self._rpc(server, "initialize")
        self.assertIn("tools", initialized["capabilities"])
        tools = self._rpc(server, "tools/list")["tools"]
        self.assertEqual({tool["name"] for tool in tools}, TOOL_NAMES)
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        self.assertEqual(schemas["desire_event"]["required"], ["event_type"])
        self.assertEqual(schemas["desire_resolve_thought"]["required"], ["thought_text"])

        status = self._call(server, "desire_status")
        self._assert_summary(status)
        self.assertEqual(status["tick_count"], 12)
        before = self.Engine(str(self.db_path)).load_state()
        self._call(server, "desire_event", {"event_type": "task_done"})
        after_event = self.Engine(str(self.db_path)).load_state()
        self.assertEqual(after_event.tick_count, before.tick_count)
        self.assertLess(after_event.drives["duty"].value, before.drives["duty"].value)
        self.assertGreater(after_event.drives["curiosity"].value, before.drives["curiosity"].value)

        tick_result = self._call(self.mcp_module.DesireMCPServer(), "desire_tick")
        self.assertIsInstance(tick_result, dict)
        after_tick = self.Engine(str(self.db_path)).load_state()
        self.assertEqual(after_tick.tick_count, after_event.tick_count + 1)
        self.assertNotEqual(after_tick.last_tick, "")
        self.assertNotEqual(after_tick.drives["curiosity"].baseline, after_event.drives["curiosity"].baseline)
        joy_before_resolve = after_tick.drives["joy"].value

        resolved = self._call(
            self.mcp_module.DesireMCPServer(),
            "desire_resolve_thought",
            {"thought_text": "sample diagram"},
        )
        self.assertIs(resolved["resolved"], True)
        reopened = self.Engine(str(self.db_path)).load_state()
        self.assertFalse(any(t.content == "Review the sample diagram" and not t.resolved for t in reopened.thoughts))
        self.assertAlmostEqual(reopened.drives["joy"].value, joy_before_resolve + 3, places=2)
        unmatched = self._call(
            self.mcp_module.DesireMCPServer(),
            "desire_resolve_thought",
            {"thought_text": "sample diagram"},
        )
        self.assertIs(unmatched["resolved"], False)
        self.assertAlmostEqual(self.Engine(str(self.db_path)).load_state().drives["joy"].value, reopened.drives["joy"].value)
        self._assert_summary(self._call(self.mcp_module.DesireMCPServer(), "desire_status"))

    def test_two_database_paths_are_independent_of_integration(self):
        other_path = self.case_dir / "other.db"
        first = self.Engine(str(self.db_path))
        second = self.Engine(str(other_path))
        first.save_state(self._seed())
        other_state = self._seed()
        other_state.tick_count = 3
        other_state.thoughts[0].content = "Inspect the sample grid"
        other_state.drives["joy"].value = 31.0
        second.save_state(other_state)

        first.trigger_event("discovery")
        first.tick()
        first.resolve("sample diagram")
        first.summary(catch_up=False)
        preserved = self.Engine(str(other_path)).load_state()
        self.assertEqual(preserved.tick_count, 3)
        self.assertEqual(preserved.drives["joy"].value, 31.0)
        self.assertEqual([t.content for t in preserved.thoughts], ["Inspect the sample grid"])
        self.assertEqual(self.integration.DB_PATH, str(self.integration_sentinel))
        self.assertNotEqual(self.Engine(str(self.db_path)).load_state().tick_count, preserved.tick_count)

    def _check_catch_up(self, naive):
        clock = datetime.now().astimezone() if naive else datetime.now(TZ_MSK)
        initial_time = clock - timedelta(seconds=7300)
        state = DesireState(tick_count=7)
        state.last_tick = (initial_time.replace(tzinfo=None) if naive else initial_time).isoformat()
        engine = self.Engine(str(self.db_path))
        engine.save_state(state)
        if naive:
            engine.tick_if_due()
        else:
            self._assert_summary(engine.summary())
        saved = self.Engine(str(self.db_path)).load_state()
        completed = saved.tick_count - 7
        self.assertGreaterEqual(completed, 2)
        self.assertLessEqual(completed, 8)
        advanced = datetime.fromisoformat(saved.last_tick)
        if advanced.tzinfo is None:
            advanced = advanced.astimezone(TZ_MSK)
        consumed = (advanced - initial_time).total_seconds()
        self.assertGreaterEqual(consumed, 900 * completed - 1)
        self.assertLessEqual(consumed, 2700 * completed + 1)
        remainder = (datetime.now(TZ_MSK) - advanced).total_seconds()
        self.assertGreater(remainder, 60, "Catch-up discarded the remaining partial interval")
        self.assertLess(remainder, calculate_tick_interval(saved) + 2)

        self.Engine(str(self.db_path)).tick_if_due()
        repeated = self.Engine(str(self.db_path)).load_state()
        self.assertEqual(repeated.tick_count, saved.tick_count)
        self.assertEqual(repeated.last_tick, saved.last_tick)

    def test_due_aware_timestamp_preserves_remainder(self):
        self._check_catch_up(naive=False)

    def test_due_naive_timestamp_preserves_remainder(self):
        self._check_catch_up(naive=True)

    def test_fresh_naive_host_time_does_not_catch_up_or_delay_for_hours(self):
        engine = self.Engine(str(self.db_path))
        state = self._seed()
        state.last_tick = datetime.now().isoformat()
        engine.save_state(state)
        summary = engine.summary()
        self.assertEqual(summary["tick_count"], state.tick_count)
        remaining = engine.seconds_until_due()
        interval = calculate_tick_interval(state)
        self.assertGreater(remaining, interval - 10)
        self.assertLessEqual(remaining, interval + 1)

    def test_catch_up_respects_tick_limit_and_keeps_backlog(self):
        anchor = datetime.now(timezone.utc) - timedelta(days=2)
        state = DesireState(last_tick=anchor.isoformat(), tick_count=4)
        engine = self.Engine(str(self.db_path))
        engine.save_state(state)
        engine.tick_if_due(max_ticks=2)
        first = self.Engine(str(self.db_path)).load_state()
        self.assertEqual(first.tick_count, 6)
        first_time = datetime.fromisoformat(first.last_tick)
        if first_time.tzinfo is None:
            first_time = first_time.replace(tzinfo=TZ_MSK)
        self.assertLess(first_time, datetime.now(timezone.utc) - timedelta(days=1))
        self.Engine(str(self.db_path)).tick_if_due(max_ticks=1)
        second = self.Engine(str(self.db_path)).load_state()
        self.assertEqual(second.tick_count, 7)
        self.assertNotEqual(second.last_tick, first.last_tick)

    def test_real_loopback_http_health_tools_and_status(self):
        http_module = importlib.import_module("mcp_http_server")
        self._patch(patch.object(http_module, "AUTH_TOKEN", ""))
        self._patch(patch.dict(os.environ, {"DESIRE_MCP_LOG": ""}))
        self.Engine(str(self.db_path)).save_state(self._seed())
        server = http_module.DesireMCPHTTPServer(("127.0.0.1", 0), http_module.DesireMCPHTTPHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        thread.start()
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIs(json.loads(response.read())["ok"], True)
            for request_id, method, params in (
                (21, "tools/list", None),
                (22, "tools/call", {"name": "desire_status", "arguments": {}}),
            ):
                message = {"jsonrpc": "2.0", "id": request_id, "method": method}
                if params is not None:
                    message["params"] = params
                connection.request("POST", "/mcp", json.dumps(message), {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read())
                self.assertEqual(payload["id"], request_id)
                self.assertEqual(payload["jsonrpc"], "2.0")
                self.assertNotIn("error", payload)
                if method == "tools/list":
                    self.assertEqual({tool["name"] for tool in payload["result"]["tools"]}, TOOL_NAMES)
                else:
                    self._assert_summary(json.loads(payload["result"]["content"][0]["text"]))
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.Engine(str(self.db_path)).load_state().tick_count, 12)

    def test_existing_integration_format_preserves_drive_and_thought_data(self):
        original = self._seed()
        original.thoughts[0].hit_count = 2
        original.thoughts[0].is_obsession = True
        original.drives["curiosity"].growth_rate = 0.37
        with patch.object(self.integration, "DB_PATH", str(self.db_path)):
            self.integration.init_tables()
            self.integration.save_state(original)

        engine = self.Engine(str(self.db_path))
        loaded = engine.load_state()
        self.assertEqual(loaded.tick_count, original.tick_count)
        self.assertEqual(loaded.last_tick, original.last_tick)
        self.assertEqual(loaded.thoughts, original.thoughts)
        self.assertEqual(loaded.drives, original.drives)
        self._assert_summary(engine.summary(catch_up=False))
        engine.trigger_event("discovery")
        with patch.object(self.integration, "DB_PATH", str(self.db_path)):
            reloaded = self.integration.load_state()
        self.assertEqual(reloaded.thoughts, original.thoughts)
        self.assertEqual(reloaded.drives["curiosity"].growth_rate, 0.37)
        self.assertGreater(reloaded.drives["joy"].value, original.drives["joy"].value)

    def test_legacy_numeric_schema_preserves_extra_data_and_resolves_naive_thought(self):
        defaults = create_default_drives()
        now = datetime.now().astimezone()
        legacy_thought = {
            "content": "Compare the sample shapes",
            "source": "reflection",
            "count": 2,
            "obsession": False,
            "created_at": now.replace(tzinfo=None).isoformat(),
            "last_hit": now.replace(tzinfo=None).isoformat(),
        }
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("""
                CREATE TABLE desire_state (
                    id INTEGER PRIMARY KEY,
                    drives_json TEXT NOT NULL,
                    baselines_json TEXT NOT NULL,
                    thoughts_json TEXT NOT NULL,
                    last_tick TEXT,
                    tick_count INTEGER DEFAULT 0,
                    extra_note TEXT DEFAULT 'default marker'
                )
            """)
            connection.execute("CREATE TABLE unrelated_metadata (value TEXT NOT NULL)")
            connection.execute("INSERT INTO unrelated_metadata VALUES ('synthetic metadata')")
            connection.execute(
                "INSERT INTO desire_state (id, drives_json, baselines_json, thoughts_json, last_tick, tick_count, extra_note) VALUES (1, ?, ?, ?, ?, ?, 'keep this marker')",
                (json.dumps({name: drive.value for name, drive in defaults.items()}),
                 json.dumps({name: drive.baseline for name, drive in defaults.items()}),
                 json.dumps([legacy_thought]), now.isoformat(), 19),
            )
            columns_before = [row[1] for row in connection.execute("PRAGMA table_info(desire_state)")]
            connection.commit()

        engine = self.Engine(str(self.db_path))
        loaded = engine.load_state()
        self.assertEqual(loaded.tick_count, 19)
        self.assertEqual(len(loaded.thoughts), 1)
        self.assertEqual(loaded.thoughts[0].source_drive, "reflection")
        self.assertEqual(loaded.thoughts[0].hit_count, 2)
        self.assertFalse(loaded.thoughts[0].is_obsession)
        self._assert_summary(engine.summary(catch_up=False))
        engine.trigger_event("task_done")
        result = engine.resolve("sample shapes")
        self.assertIs(result["resolved"], True)
        reopened = self.Engine(str(self.db_path)).load_state()
        self.assertFalse(any(t.content == legacy_thought["content"] and not t.resolved for t in reopened.thoughts))
        self.assertEqual(reopened.tick_count, 19)
        self.assertGreater(reopened.drives["joy"].value, defaults["joy"].value)

        with closing(sqlite3.connect(self.db_path)) as connection:
            columns_after = [row[1] for row in connection.execute("PRAGMA table_info(desire_state)")]
            row = connection.execute("SELECT drives_json, baselines_json, thoughts_json, extra_note FROM desire_state WHERE id=1").fetchone()
            self.assertEqual(columns_after, columns_before)
            self.assertEqual(row[3], "keep this marker")
            self.assertEqual(connection.execute("SELECT value FROM unrelated_metadata").fetchone()[0], "synthetic metadata")
        self.assertTrue(all(isinstance(value, (int, float)) for value in json.loads(row[0]).values()))
        self.assertEqual(set(json.loads(row[1])), set(defaults))
        for saved_thought in json.loads(row[2]):
            self.assertEqual(set(saved_thought), set(legacy_thought))


if __name__ == "__main__":
    unittest.main()
