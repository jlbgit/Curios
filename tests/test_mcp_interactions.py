"""
Live ChromaDB smoke + concurrency stress for MCP tool handlers.

Deterministic coverage lives in test_server.py (mocked) and
test_integration.py (synthetic index). This module only keeps:

- Live smoke: real populated index, cross-project search + recap
- Concurrent curios_search calls (race / stability check)

Needs populated Chroma (CURIOS_DATA) only; no tests/eval/.env required.

Usage:
    uv run pytest -m live -v
"""

from __future__ import annotations

import threading
import time

import pytest

from curios.config import CHROMADB_PATH, COLLECTION_NAME
from curios.server import curios_recap, curios_search
from tests.conftest import unwrap_curios_result


@pytest.fixture(scope="module")
def db_populated():
    try:
        import chromadb

        client = chromadb.PersistentClient(path=str(CHROMADB_PATH))
        coll = client.get_collection(name=COLLECTION_NAME)
        count = coll.count()
    except Exception:
        count = 0
    if count == 0:
        pytest.skip("ChromaDB collection is empty — run curios-index first")
    return count


@pytest.mark.live
class TestLiveSmoke:
    def test_search_and_recap_against_real_index(self, db_populated):
        raw_search = curios_search(query="architecture decisions", n_results=3)
        search_data = unwrap_curios_result(raw_search)
        assert "by_project" in search_data
        total = sum(len(v) for v in search_data["by_project"].values())
        assert total > 0

        raw_recap = curios_recap(n_results=3)
        recap_data = unwrap_curios_result(raw_recap)
        assert recap_data.get("recap_project") == "(all)"
        assert len(recap_data.get("recent_conversations") or []) > 0


@pytest.mark.live
class TestConcurrency:
    def test_concurrent_queries_no_crash(self, db_populated):
        """Multiple threads querying simultaneously should not raise errors."""
        errors: list[str] = []
        queries = ["architecture", "decisions", "test query", "ideas", "problems"]

        def worker(query: str, iterations: int = 10):
            for _ in range(iterations):
                try:
                    curios_search(query=query, n_results=3)
                except Exception as e:
                    errors.append(f"{query}: {type(e).__name__}: {e}")
                time.sleep(0.01)

        threads = [threading.Thread(target=worker, args=(q,)) for q in queries]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Concurrent query errors: {errors}"
