"""Synthetic local retrieval benchmark. No services, real data or model calls.

Run: .venv/bin/python tests/benchmark_project_knowledge.py
These exact-identifier cases measure performance/regressions, not real-world QA.
"""
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import project_knowledge as knowledge
import storage
from task_store import TaskStore


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        documents = [{'id': f'doc-{i}', 'title': f'Interface cedar_{i:04d}', 'tags': [],
                      'content': ('General integration background and error handling.\n' * 40
                                  + f'\ncedar_{i:04d} requires retry budget {i % 5 + 1}. Preserve its accepted interface.\n')}
                     for i in range(1500)]
        data_file = root / 'data.json'
        data_file.write_text(json.dumps({'stores': [{'id': 'benchmark', 'name': 'Benchmark', 'documents': documents}]}))
        with patch.object(storage, 'DATA_FILE', data_file), patch.object(storage, 'DATA_DIR', root):
            service = knowledge.KnowledgeService(TaskStore(root / 'runs.json'))
            started = time.perf_counter()
            service.search('benchmark', 'cedar_0000 retry budget')
            cold = (time.perf_counter() - started) * 1000
            timings, counts, correct = [], [], 0
            for i in range(40):
                index = i * 37
                started = time.perf_counter()
                result = service.search('benchmark', f'cedar_{index:04d} retry budget')
                timings.append((time.perf_counter() - started) * 1000)
                counts.append(result['tokens'])
                correct += bool(result['results'] and f'cedar_{index:04d}' in result['results'][0]['title'])
            print(json.dumps({'synthetic_documents': len(documents), 'source_characters': service.snapshot('benchmark').characters,
                'cold_ms': round(cold, 2), 'warm_p50_ms': round(statistics.median(timings), 2),
                'warm_p95_ms': round(sorted(timings)[int(len(timings) * .95) - 1], 2),
                'top_source_correct': f'{correct}/40 exact-identifier cases',
                'max_context_tokens': max(counts), 'retrieval_model_calls': 0}, indent=2))


if __name__ == '__main__':
    main()
