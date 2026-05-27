import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from nanobot.agent.retriever import MemoryRetriever


def _make_comp(i):
    return {
        "id": f"doc-{i}",
        "summary_full": f"Concurrent test doc with terms: alpha beta gamma delta {i}",
        "meta": {"session_key": f"s{i}"},
        "updated_at": time.time(),
    }


def test_concurrent_writes_and_queries(tmp_path: Path):
    r = MemoryRetriever()
    idx_path = tmp_path / "retriever_index.json"

    writers = 50
    readers = 50

    def writer(i):
        comp = _make_comp(i)
        # incremental update
        r.index_compactions([comp], replace=False)
        # schedule async persist
        r.schedule_persist(idx_path, retries=1, backoff=0.01)
        return comp["id"]

    def reader(term):
        return r.query(term, top_k=5)

    # run writers and readers concurrently
    with ThreadPoolExecutor(max_workers=16) as ex:
        futures = []
        for i in range(writers):
            futures.append(ex.submit(writer, i))
        for i in range(readers):
            # query for a term that should exist in most docs
            futures.append(ex.submit(reader, "alpha"))

        results = []
        for fut in as_completed(futures, timeout=10):
            results.append(fut.result())

    # wait for background writer queue to finish
    # the implementation exposes _write_queue which is a queue.Queue
    # join it to ensure all scheduled persists completed
    try:
        q = getattr(r, "_write_queue", None)
        if q is not None:
            q.join()
    except Exception:
        pass

    # a few sanity assertions
    assert len(r._docs) >= writers
    # queries returned lists for reader tasks — ensure at least one hit exists
    some_hits = any(isinstance(x, list) and x for x in results)
    assert some_hits
