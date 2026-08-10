import unittest

import random
import tempfile
from pathlib import Path

from desire.core import DRIVE_CONFIG, DesireState, Thought
from desire.thoughts import generate_thoughts, reinforce_obsessions, resolve_thought
from desire.tick import run_tick, update_baselines
from desire.integration import DesireEngine


class DesireStabilityTests(unittest.TestCase):
    def test_obsessions_stop_at_action_threshold(self):
        state = DesireState()
        state.drives["curiosity"] = 59
        state.thoughts = [
            Thought("继续研究", "curiosity", count=3, obsession=True),
            Thought("再看一点", "curiosity", count=3000, obsession=True),
        ]
        reinforce_obsessions(state)
        self.assertEqual(state.drives["curiosity"], DRIVE_CONFIG["curiosity"]["threshold"])
        reinforce_obsessions(state)
        self.assertEqual(state.drives["curiosity"], DRIVE_CONFIG["curiosity"]["threshold"])

    def test_obsession_push_decays_with_hit_count(self):
        recent = DesireState()
        repeated = DesireState()
        recent.thoughts = [Thought("新执念", "curiosity", count=3, obsession=True)]
        repeated.thoughts = [Thought("旧执念", "curiosity", count=3000, obsession=True)]
        reinforce_obsessions(recent)
        reinforce_obsessions(repeated)
        self.assertGreater(recent.drives["curiosity"], repeated.drives["curiosity"])

    def test_baseline_ema_remains_anchored(self):
        state = DesireState()
        state.drives["curiosity"] = 100
        for _ in range(3000):
            update_baselines(state)
        self.assertLess(state.baselines["curiosity"], 65)
        self.assertGreaterEqual(state.baselines["curiosity"], DRIVE_CONFIG["curiosity"]["baseline"])

    def test_intimacy_can_fall_above_baseline(self):
        config = DRIVE_CONFIG["intimacy"]
        self.assertLess(config["growth"], config["decay"])

    def test_joy_does_not_erase_fatigue(self):
        state = DesireState()
        state.drives["joy"] = 100
        state.drives["fatigue"] = 10
        run_tick(state)
        self.assertGreater(state.drives["fatigue"], 0)

    def test_joy_does_not_erase_stress(self):
        state = DesireState()
        state.drives["joy"] = 100
        state.drives["stress"] = 10
        run_tick(state)
        self.assertGreater(state.drives["stress"], 0)

    def test_resolved_source_does_not_immediately_regenerate(self):
        state = DesireState()
        state.drives["curiosity"] = 90
        state.thoughts = [Thought("这个问题还可以继续挖。", "curiosity", count=3, obsession=True)]
        self.assertTrue(resolve_thought(state, "继续挖"))
        generate_thoughts(state, rng=random.Random(0))
        self.assertFalse(any(item.source == "curiosity" for item in state.thoughts))
        state.drives["curiosity"] = 59
        generate_thoughts(state, rng=random.Random(0))
        self.assertNotIn("curiosity", state.resolved_sources)

    def test_resolved_sources_survive_engine_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = DesireEngine(str(Path(directory) / "desire.db"))
            state = engine.load_state()
            state.drives["curiosity"] = 90
            state.thoughts = [Thought("继续研究", "curiosity", count=3, obsession=True)]
            engine.save_state(state)
            self.assertTrue(engine.resolve("继续研究")["resolved"])
            loaded = engine.load_state()
            self.assertIn("curiosity", loaded.resolved_sources)


if __name__ == "__main__":
    unittest.main()
