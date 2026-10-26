"""Knowledge saves are isolated from user data and safe across clients/processes."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import storage


def add_from_process(directory, store_id, worker):
    storage.DATA_DIR = Path(directory)
    storage.DATA_FILE = storage.DATA_DIR / "data.json"
    for index in range(8):
        storage.create_link(store_id, f"{worker}-{index}", "https://example.test")


class ConcurrentKnowledgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for patcher in (patch.object(storage, "DATA_DIR", self.directory),
                        patch.object(storage, "DATA_FILE", self.directory / "data.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.project = storage.create_store("Shared project")

    def test_simultaneous_saves_keep_every_document_and_unique_slug(self):
        original = storage._load
        def delayed_load():
            data = original()
            time.sleep(0.002)  # Expose an unlocked read/modify/write race reliably.
            return data
        with patch.object(storage, "_load", side_effect=delayed_load), ThreadPoolExecutor(max_workers=6) as pool:
            documents = list(pool.map(lambda n: storage.create_document(self.project["id"], str(n), "shared note"), range(24)))
            stores = list(pool.map(lambda _: storage.create_store("Same name"), range(12)))
        self.assertEqual({d["id"] for d in storage.list_documents(self.project["id"])}, {d["id"] for d in documents})
        self.assertEqual(len({s["slug"] for s in stores}), 12)

    def test_separate_processes_do_not_lose_each_others_changes(self):
        context = multiprocessing.get_context("spawn")
        workers = [context.Process(target=add_from_process, args=(str(self.directory), self.project["id"], n)) for n in range(3)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(len(storage.list_links(self.project["id"])), 24)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)

    def test_failed_replace_retains_old_data_and_removes_temporary_file(self):
        previous = storage.DATA_FILE.read_bytes()
        with patch.object(storage.os, "replace", side_effect=OSError("disk error")), self.assertRaises(OSError):
            storage.create_document(self.project["id"], "Not saved", "draft")
        self.assertEqual(storage.DATA_FILE.read_bytes(), previous)
        self.assertEqual(list(self.directory.glob(".data.json.*")), [])
        self.assertEqual(len(json.loads(previous)["stores"]), 2)
        storage.create_document(self.project["id"], "Retry", "saved")
        self.assertEqual(len(storage.list_documents(self.project["id"])), 1)
