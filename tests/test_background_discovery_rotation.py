"""Independent offline tests of durable category scheduling and request concurrency."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path

from engine.background_candidates import discover_candidates
from engine.backgrounds import ALLOWED_NATURE_QUERIES, BACKGROUND_SEARCH_CATEGORIES, BackgroundDiscoveryPage
from tests.test_background_candidates import InMemCandidateStore, make_candidate


class DurableStore(InMemCandidateStore):
    """JSON simulates the remote builder row, independently of client instances."""
    def __init__(self, path, pending=0):
        super().__init__(pending=pending)
        self.path = Path(path)

    def get_builder_run(self, run_id):
        return json.loads(self.path.read_text()) if self.path.exists() else None

    def save_builder_run(self, run_id, status, value):
        self.path.write_text(json.dumps({"builder_run_id": run_id, "status": status, "value": value}))


class EmptyProvider:
    def search(self, query, page=1, **kwargs):
        return BackgroundDiscoveryPage("pexels", query, page, 0, (), ())


class TestDiscoveryRotation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "checkpoint.json"

    def run_empty(self, **kwargs):
        return discover_candidates(store=DurableStore(self.state_path),
            providers={"pexels": EmptyProvider()}, max_api_requests=2,
            logger=lambda _: None, **kwargs)

    def test_catalog_queries_have_exactly_one_normalized_owner(self):
        terms = [q for _, queries in BACKGROUND_SEARCH_CATEGORIES for q in queries]
        self.assertEqual(len(terms), len(set(terms)))
        self.assertEqual(set(terms), set(ALLOWED_NATURE_QUERIES))
        self.assertTrue(all(q == q.strip().lower() and "#" not in q for q in terms))
        self.assertTrue({"forest", "trees", "autumn", "mountains", "sunset", "ocean", "landscape"} <= set(terms))

    def test_equal_category_cycles_persist_across_new_clients_even_without_results(self):
        categories = [name for name, _ in BACKGROUND_SEARCH_CATEGORIES]
        observed = [self.run_empty()["rotation"]["category"] for _ in range(2 * len(categories))]
        self.assertEqual(observed, categories * 2)
        self.assertEqual(set(Counter(observed).values()), {2})
        self.assertEqual(json.loads(self.state_path.read_text())["value"]["sequence"], 2 * len(categories))

    def test_category_query_and_page_progress_resume_on_next_category_visit(self):
        first = self.run_empty()
        for _ in range(len(BACKGROUND_SEARCH_CATEGORIES) - 1):
            self.run_empty()
        second = self.run_empty()
        self.assertEqual(first["query_terms_touched"], ["forest", "trees"])
        self.assertEqual(second["query_terms_touched"], ["autumn", "autumn forest"])
        self.assertEqual(first["pages_touched"], [1])
        self.assertEqual(second["pages_touched"], [2])

    def test_cursor_survives_actual_process_restart(self):
        script = """
import sys
from tests.test_background_discovery_rotation import DurableStore, EmptyProvider
from engine.background_candidates import discover_candidates
result = discover_candidates(store=DurableStore(sys.argv[1]), providers={'pexels': EmptyProvider()}, max_api_requests=1, logger=lambda _: None)
print(result['rotation']['category'])
"""
        results = [subprocess.run([sys.executable, "-c", script, str(self.state_path)],
                   check=True, capture_output=True, text=True).stdout.strip() for _ in range(2)]
        self.assertEqual(results, ["forest_trees_autumn", "mountains"])

    def test_four_requests_in_flight_and_reverse_completion_preserve_enqueue_order(self):
        barrier = threading.Barrier(4, timeout=5)
        completed = []
        lock = threading.Lock()
        # The four responses complete in reverse request order without timed sleeps.
        events = [threading.Event() for _ in range(4)]
        events[3].set()

        class Provider:
            def __init__(self, name): self.name = name
            def search(self, query, page=1, **kwargs):
                index = (0 if query == "forest" else 2) + (self.name == "pixabay")
                barrier.wait()
                if not events[index].wait(5):
                    raise AssertionError("parallel response chain stalled")
                with lock: completed.append(index)
                if index: events[index - 1].set()
                candidate = make_candidate(self.name, str(index + 1), query=query)
                return BackgroundDiscoveryPage(self.name, query, page, 1, (candidate,), ())

        store = DurableStore(self.state_path)
        result = discover_candidates(store=store,
            providers={name: Provider(name) for name in ("pexels", "pixabay")},
            max_api_requests=4, max_search_workers=100, logger=lambda _: None)
        self.assertEqual(result["provider_errors"], 0)
        self.assertEqual(completed, [3, 2, 1, 0])
        self.assertEqual(result["provider_requests"], 4)
        self.assertEqual([(r["search_query"], r["provider"]) for r in store.rows.values()],
                         [("forest", "pexels"), ("forest", "pixabay"),
                          ("trees", "pexels"), ("trees", "pixabay")])

    def test_request_cap_is_absolute(self):
        result = discover_candidates(store=DurableStore(self.state_path),
            providers={"pexels": EmptyProvider(), "pixabay": EmptyProvider()},
            max_api_requests=10000, max_search_workers=10000, logger=lambda _: None)
        self.assertEqual(result["provider_requests"], 80)
        self.assertTrue(set(result["pages_touched"]) <= {1, 2, 3, 4, 5})

    def test_metadata_cap_does_not_depend_on_pending_backlog(self):
        class ManyProvider:
            def search(self, query, page=1, **kwargs):
                values = tuple(make_candidate(asset=str(i), query=query) for i in range(1, 1002))
                return BackgroundDiscoveryPage("pexels", query, page, len(values), values, ())

        for pending, metadata_limit, expected in ((98, 1000, 1000), (0, 3, 3)):
            with self.subTest(pending=pending):
                result = discover_candidates(store=DurableStore(self.state_path, pending=pending),
                    providers={"pexels": ManyProvider()}, max_candidates=1000,
                    max_metadata_items=metadata_limit, max_api_requests=4, logger=lambda _: None)
                self.assertEqual(result["discovered"], expected)
                self.assertEqual(result["metadata_records_examined"], expected)
                self.assertEqual(result["summary"]["pending"], pending + expected)

    def test_large_pending_queue_still_searches_and_rotates_checkpoint(self):
        result = discover_candidates(store=DurableStore(self.state_path, pending=1000),
            providers={"pexels": EmptyProvider()}, logger=lambda _: None)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertGreater(result["provider_requests"], 0)
        self.assertLessEqual(result["provider_requests"], 80)
        self.assertTrue(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
