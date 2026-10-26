import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import task_usage


class UsageTests(unittest.TestCase):
    def test_late_counts_follow_original_turn_and_unknown_cost_is_not_zero(self):
        run = {'id': 'r', 'turn_id': 'new', 'usage': {'model_calls': 3, 'input_tokens': 40, 'output_tokens': 10, 'unknown_calls': 1},
               'turns': [{'turn_id': 'old', 'usage': {'model_calls': 2}, 'call_details': [
                   {'call_id': 1, 'provider': 'p', 'model': 'a', 'stage': 'worker'},
                   {'call_id': 2, 'provider': 'p', 'model': 'b', 'stage': 'review'}]}],
               'call_details': [{'call_id': 3, 'provider': 'p', 'model': 'a', 'stage': 'answer'}]}
        ledger = {'1': {'input_tokens': 10, 'output_tokens': 5},
                  '2': {'input_tokens': 20, 'output_tokens': None},
                  '3': {'input_tokens': 10, 'output_tokens': 5}}
        result = task_usage.summarize(run, ledger, {'p/a': {'input_per_million': 1, 'output_per_million': 2}})
        self.assertEqual(result['turns'][0]['input_tokens'], 30)
        self.assertEqual(result['turns'][1]['input_tokens'], 10)
        self.assertEqual(result['total']['unpriced_calls'], 1)
        self.assertAlmostEqual(result['total']['estimated_cost_usd'], .00004)
        self.assertIsNone(result['models'][1]['estimated_cost_usd'])

    def test_legacy_totals_are_preserved_without_inventing_model_or_price(self):
        run = {'id': 'r', 'usage': {'model_calls': 2, 'input_tokens': 50, 'output_tokens': 10, 'unknown_calls': 0}}
        result = task_usage.summarize(run, {}, {'p/a': {'input_per_million': 1, 'output_per_million': 2}})
        self.assertEqual(result['total']['input_tokens'], 50)
        self.assertEqual(result['unattributed_tokens'], {'input_tokens': 50, 'output_tokens': 10})
        self.assertIsNone(result['models'][0]['model'])
        self.assertIsNone(result['total']['estimated_cost_usd'])

    def test_explicit_zero_price_is_distinct_from_unknown_price(self):
        run = {'id': 'r', 'usage': {'model_calls': 1, 'input_tokens': 10, 'output_tokens': 2, 'unknown_calls': 0},
               'call_details': [{'call_id': 1, 'provider': 'p', 'model': 'local'}]}
        result = task_usage.summarize(run, {'1': {'input_tokens': 10, 'output_tokens': 2}},
                                      {'p/local': {'input_per_million': 0, 'output_per_million': 0}})
        self.assertEqual(result['total']['estimated_cost_usd'], 0)
        self.assertEqual(result['total']['unpriced_calls'], 0)

    def test_empty_task_does_not_turn_unknown_project_cost_into_zero(self):
        unknown = task_usage.summarize({'id': 'r', 'usage': {'model_calls': 1, 'input_tokens': 1, 'output_tokens': 1, 'unknown_calls': 0}}, {}, {})
        empty = task_usage.summarize({'id': 'empty', 'usage': {'model_calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'unknown_calls': 0}}, {}, {})
        result = task_usage.project_totals([unknown, empty])
        self.assertIsNone(result['estimated_cost_usd'])
