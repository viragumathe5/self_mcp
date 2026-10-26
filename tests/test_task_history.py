"""Conversation context bounds: latest work survives, omitted history stays saved."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import providers
import task_history
import task_memory


class ConversationContextTests(unittest.TestCase):
    def setUp(self):
        encoder = Mock()
        encoder.encode.side_effect = lambda text, **_: text.split()
        patcher = patch.object(task_history.tokenization, "get_encoding", return_value=encoder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_long_history_retains_latest_answer_and_original_goal(self):
        run = {"root_goal": "Build a reading list", "turns": [
            {"goal": f"Change {i}", "final_output": (f"answer{i} " * 100), "status": "completed"} for i in range(20)]}
        original = deepcopy(run)
        result = task_history.conversation(run, "Follow up", {"goal": "Add tests"}, {"context_tokens": 1200, "output_tokens": 256})
        self.assertEqual(result["original_goal"], run["root_goal"])
        self.assertGreater(result["omitted_older_turns"], 0)
        self.assertEqual(result["turns"][-1]["assistant"], run["turns"][-1]["final_output"])
        self.assertEqual(run, original, "Context selection must not erase stored history")

    def test_failed_turn_preserves_previous_success_and_partial_work(self):
        run = {"turns": [{"goal": "Initial", "final_output": "Working version", "status": "completed"},
                         {"goal": "Add tests", "final_output": None, "status": "failed",
                          "tasks": [{"title": "Tests", "output": "Partially drafted tests"}]}]}
        result = task_history.conversation(run, "Follow up", {"goal": "Continue"})
        self.assertEqual(result["turns"][0]["assistant"], "Working version")
        self.assertEqual(result["turns"][1]["partial_drafts"][0]["draft"], "Partially drafted tests")

    def test_latest_answer_is_never_silently_dropped_to_fit(self):
        run = {"turns": [{"goal": "Initial", "final_output": "token " * 2000, "status": "completed"}]}
        with self.assertRaises(providers.ProviderError) as raised:
            task_history.conversation(run, "Follow up", {}, {"context_tokens": 1000, "output_tokens": 256})
        self.assertIn("larger context window", raised.exception.message)

    def test_current_goal_and_attached_context_share_the_budget(self):
        run = {"turns": [{"goal": "Initial", "final_output": "Short answer", "status": "completed"}]}
        with self.assertRaises(providers.ProviderError):
            task_history.conversation(run, "Follow up", {"goal": "word " * 900}, {"context_tokens": 1000, "output_tokens": 256})


class CollaborationMemoryTests(unittest.TestCase):
    def test_memory_preserves_source_and_is_bounded_without_erasing_storage(self):
        run = {"turns": [{"turn_id": str(i), "reviews": [{"decisions": [f"Decision {i}-{j}" for j in range(8)]}],
                          "tasks": [{"id": "a", "title": "Code", "output": "x" * 600, "output_version": 2}]} for i in range(20)]}
        original = deepcopy(run)
        result = task_memory.context(run)
        self.assertEqual(len(result["decisions"]), 8)
        self.assertEqual(result["decisions"][0]["turn_id"], "19")
        self.assertEqual(len(result["prior_work"]), 6)
        self.assertEqual(result["omitted_artifacts"], 14)
        self.assertTrue(result["prior_work"][0]["excerpt_truncated"])
        self.assertEqual(run, original)

    def test_direct_or_legacy_turns_without_reviews_still_work(self):
        result = task_memory.context({"turns": [{"id": "old", "final_output": "Answer"}]})
        self.assertEqual(result["decisions"], [])
        self.assertIsNone(result["latest_review"])

    def test_current_review_and_plan_are_visible_to_other_workers(self):
        run = {"turn_id": "current", "tasks": [{"id": "a", "title": "Interface", "depends_on": [], "status": "completed", "output_version": 1}],
               "reviews": [{"feedback": "Fix one boundary case", "decisions": ["Keep JSON"], "task_reviews": []}]}
        result = task_memory.context(run)
        self.assertEqual(result["current_plan"][0]["output_version"], 1)
        self.assertEqual(result["latest_review"]["feedback"], "Fix one boundary case")
        self.assertEqual(result["decisions"][0]["turn_id"], "current")

    def test_new_review_cannot_erase_prior_decision_by_omitting_it(self):
        run = {"turn_id": "current", "reviews": [{"decisions": ["Keep JSON"]}, {"decisions": []}]}
        result = task_memory.context(run)
        self.assertEqual(result["decisions"][0]["decision"], "Keep JSON")
        self.assertEqual(result["decisions"][0]["review"], 1)
