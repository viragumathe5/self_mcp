"""First use must work offline and retain exact cl100k_base counting."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import tokenization
import project_knowledge
import task_models
import provider_transport


class OfflineTokenizerTests(unittest.TestCase):
    def tearDown(self):
        tokenization.get_encoding.cache_clear()
        provider_transport._encoding.cache_clear()

    def test_first_use_needs_no_network_or_external_cache(self):
        tokenization.get_encoding.cache_clear()
        provider_transport._encoding.cache_clear()
        with patch("socket.socket", side_effect=AssertionError("No network allowed")), \
             patch("tiktoken.get_encoding", side_effect=AssertionError("No downloader allowed")):
            encoding = tokenization.get_encoding()
            self.assertEqual(encoding.encode("hello world"), [15339, 1917])
            text = "Knowledge: 日本語, αβ, café, 🧪 <|endoftext|>"
            self.assertEqual(encoding.decode(encoding.encode(text, disallowed_special=())), text)
            self.assertEqual(project_knowledge.token_count("hello world"), 2)
            task_models.request_input("Answer", {"goal": "hello world"})
            self.assertEqual(provider_transport._encoding().encode("hello world"), [15339, 1917])

    def test_corrupt_data_is_reported_without_downloading_a_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.tiktoken"
            path.write_text("corrupt")
            tokenization.get_encoding.cache_clear()
            with patch.object(tokenization, "ASSET", path), self.assertRaisesRegex(ValueError, "damaged"):
                tokenization.get_encoding()
