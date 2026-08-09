import unittest

from desire.core import DRIVE_CONFIG, DesireState, Thought
from desire.thoughts import reinforce_obsessions
from desire.tick import update_baselines


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


if __name__ == "__main__":
    unittest.main()
