import os
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event, Thread

from nanobot.agent.retriever import MemoryRetriever


# Configure via env vars for flexibility in CI/local runs
WRITERS = int(os.getenv("PERF_WRITERS", "100"))
READERS = int(os.getenv("PERF_READERS", "100"))
WRITES_PER_WRITER = int(os.getenv("PERF_WRITES_PER_WRITER", "3"))
READS_PER_READER = int(os.getenv("PERF_READS_PER_READER", "10"))


def _make_comp(i, j):
    return {
        "id": f"doc-{i}-{j}",
        "summary_full": f"Perf doc with keywords: alpha beta gamma delta {i} {j}",
        "meta": {"session_key": f"s{i}"},
        "updated_at": time.time(),
    }


def test_retriever_perf(tmp_path: Path):
    r = MemoryRetriever()
    idx_path = tmp_path / "retriever_index.json"

    # shared metrics
    write_latencies = []
    query_latencies = []
    queue_samples = []

    stop_sampler = Event()

    def sampler():
        while not stop_sampler.is_set():
            q = getattr(r, "_write_queue", None)
            if q is not None:
                try:
                    queue_samples.append(q.qsize())
                except Exception:
                    pass
            time.sleep(0.05)

    sampler_thread = Thread(target=sampler, daemon=True)
    sampler_thread.start()

    def writer_task(i):
        local_latencies = []
        for j in range(WRITES_PER_WRITER):
            comp = _make_comp(i, j)
            t0 = time.perf_counter()
            r.index_compactions([comp], replace=False)
            r.schedule_persist(idx_path, retries=1, backoff=0.01)
            local_latencies.append(time.perf_counter() - t0)
        return local_latencies

    def reader_task(i):
        local_latencies = []
        for k in range(READS_PER_READER):
            t0 = time.perf_counter()
            res = r.query("alpha")
            local_latencies.append(time.perf_counter() - t0)
            # small sleep to spread reads
            time.sleep(0.005)
        return local_latencies

    total_tasks = WRITERS + READERS
    with ThreadPoolExecutor(max_workers=64) as ex:
        futures = []
        for i in range(WRITERS):
            futures.append(ex.submit(writer_task, i))
        for i in range(READERS):
            futures.append(ex.submit(reader_task, i))

        for fut in as_completed(futures):
            res = fut.result()
            if isinstance(res, list) and res and all(isinstance(x, float) for x in res):
                # distinguish by length: writers return WRITES_PER_WRITER
                if len(res) == WRITES_PER_WRITER:
                    write_latencies.extend(res)
                else:
                    query_latencies.extend(res)

    # wait for background persists to finish
    try:
        q = getattr(r, "_write_queue", None)
        if q is not None:
            q.join()
    except Exception:
        pass

    # stop sampler and gather stats
    stop_sampler.set()
    sampler_thread.join(timeout=1)

    # basic assertions
    assert len(r._docs) >= WRITERS * WRITES_PER_WRITER

    # compute and print metrics
    def stats(name, data):
        if not data:
            print(f"{name}: no samples")
            return
        p50 = statistics.median(data)
        p90 = statistics.quantiles(data, n=10)[8]
        print(f"{name}: count={len(data)} mean={statistics.mean(data):.6f}s p50={p50:.6f}s p90={p90:.6f}s")

    stats("write_latency", write_latencies)
    stats("query_latency", query_latencies)
    if queue_samples:
        print(f"queue_samples: count={len(queue_samples)} max={max(queue_samples)} mean={statistics.mean(queue_samples):.2f}")

    # sanity
    assert write_latencies and query_latencies
