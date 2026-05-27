from __future__ import annotations

import re
import time
import json
import threading
import queue
import os
from collections import defaultdict
import math
import statistics
from typing import Dict, List, Tuple
from datetime import datetime
from pathlib import Path


_PATH_TOKEN_RE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]+")


def _tokenize(text: str) -> List[str]:
    tokens = [t for t in re.split(r"\W+", text.lower()) if t]
    path_tokens = [p.lower() for p in _PATH_TOKEN_RE.findall(text)]
    return tokens + [p for p in path_tokens if p not in tokens]


def _assess_safety(text: str | None, sections: dict | None) -> str:
    """Heuristic safety assessment for a compaction text.

    Returns one of: 'read-only', 'requires_confirmation', 'unsafe'.
    """
    if not text:
        return "read-only"
    t = text.lower()
    # obvious command executors
    if re.search(r"\b(run|execute|npm|pip|pytest|git|docker|sh|bash|curl|apply)\b", t):
        return "requires_confirmation"
    # file-edit / refactor hints
    if re.search(r"\b(edit|modify|change|refactor|replace|overwrite|apply patch)\b", t):
        return "requires_confirmation"
    # decision lines that imply state changes
    if sections and isinstance(sections, dict):
        dec = sections.get("decisions") or []
        cmds = sections.get("commands_run") or []
        if dec or cmds:
            return "requires_confirmation"
    # fallback to read-only
    return "read-only"


_CATEGORY_KEYWORDS = {
    "user_preference": (
        "prefer",
        "preference",
        "user likes",
        "用户偏好",
        "我偏好",
        "喜欢",
    ),
    "assistant_style": (
        "assistant style",
        "tone",
        "style",
        "soul",
        "语气",
        "风格",
    ),
    "failure": (
        "failure",
        "failed",
        "error",
        "traceback",
        "exception",
        "失败",
        "错误",
    ),
    "command": (
        "commands run",
        "pytest",
        "npm run",
        "ruff",
        "git ",
        "docker",
        "命令",
    ),
    "decision": (
        "decision",
        "decided",
        "we should",
        "选择",
        "决定",
    ),
}


def _section_text(sections: dict | None, key: str) -> str:
    if not isinstance(sections, dict):
        return ""
    raw = sections.get(key)
    if isinstance(raw, list):
        return " ".join(str(item) for item in raw)
    if isinstance(raw, str):
        return raw
    return ""


def _classify_memory(text: str, sections: dict | None) -> str:
    lowered = text.lower()
    if _section_text(sections, "commands_run"):
        return "command"
    if _section_text(sections, "failures"):
        return "failure"
    if _section_text(sections, "decisions"):
        return "decision"
    if any(keyword in lowered for keyword in _CATEGORY_KEYWORDS["decision"]):
        return "decision"
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return "project_fact"


def _match_reason(query_tokens: list[str], doc_text: str, sections: dict | None, category: str) -> str:
    lowered = doc_text.lower()
    path_terms = [
        token for token in query_tokens
        if "." in token or "/" in token or "\\" in token
    ]
    for term in path_terms:
        if term.lower() in lowered:
            return f"path:{term}"
    for section in ("failures", "commands_run", "decisions"):
        section_value = _section_text(sections, section).lower()
        if section_value and any(token in section_value for token in query_tokens):
            return f"section:{section}"
    matched = [token for token in query_tokens if token in lowered]
    if matched:
        return f"term:{matched[0]}"
    return f"category:{category}"


class MemoryRetriever:
    """A lightweight in-memory retriever for compaction summaries.

    MVP implementation: inverted index + TF scoring with recency boost.
    Designed to be simple and replaceable by a vector index later.
    """

    def __init__(self) -> None:
        # term -> list of (doc_id, tf)
        self._index: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        # doc_id -> doc info
        self._docs: Dict[str, Dict] = {}
        # simple lock for concurrency safety
        self._lock = threading.RLock()
        # background writer queue and thread to persist index without blocking callers
        self._write_queue: "queue.Queue[tuple[Path, int, float]]" = queue.Queue()
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    def _writer_loop(self) -> None:
        while True:
            try:
                target, retries, backoff = self._write_queue.get()
                try:
                    # simple file lock via exclusive creation of .lock file
                    lockfile = Path(str(target) + ".lock")
                    lock_fd = None
                    acquired = False
                    for attempt in range(max(1, int(retries)) + 2):
                        try:
                            # O_EXCL to ensure exclusive creation
                            fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            lock_fd = fd
                            acquired = True
                            break
                        except FileExistsError:
                            time.sleep(backoff * (attempt + 1))
                            continue
                    if not acquired:
                        # couldn't acquire lock; proceed anyway but write to backup
                        try:
                            self.persist_index(target, retries=1, backoff=backoff)
                        except Exception:
                            # fallback: write a timestamped backup
                            bak = target.with_suffix(target.suffix + f".bak.{int(time.time())}")
                            try:
                                self.persist_index(bak, retries=1, backoff=backoff)
                            except Exception:
                                pass
                    else:
                        try:
                            # perform actual persist (may raise)
                            self.persist_index(target, retries=retries, backoff=backoff)
                        finally:
                            try:
                                os.close(lock_fd)
                            except Exception:
                                pass
                            try:
                                lockfile.unlink()
                            except Exception:
                                pass
                finally:
                    self._write_queue.task_done()
            except Exception:
                # keep loop alive
                time.sleep(0.1)

    def schedule_persist(self, path: str | Path, retries: int = 3, backoff: float = 0.1) -> None:
        """Schedule an asynchronous persist of the index to *path*.

        The background writer will attempt to acquire a simple file lock and
        persist without blocking the caller.
        """
        self._write_queue.put((Path(path), int(retries), float(backoff)))

    # -- persistence -----------------------------------------------------
    def persist_index(self, path: str | Path, retries: int = 3, backoff: float = 0.1) -> None:
        """Persist the current index & docs to disk as JSON.

        Retries on transient I/O errors. Raises the last exception on final
        failure so callers can decide whether to swallow or escalate.
        """
        target = Path(path)
        payload = {
            "docs": self._docs,
            "index": {k: [[d, int(tf)] for (d, tf) in v] for k, v in self._index.items()},
        }
        tmp = target.with_suffix(target.suffix + ".tmp")
        attempt = 0
        last_exc = None
        while attempt < max(1, int(retries)):
            attempt += 1
            with self._lock:
                try:
                    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(target)
                    # success -> return
                    return
                except Exception as exc:  # pragma: no cover - retry path
                    last_exc = exc
            # small backoff before retry
            time.sleep(backoff * attempt)

        # if we get here, all attempts failed
        if last_exc is not None:
            raise last_exc

    def load_index(self, path: str | Path) -> None:
        """Load a previously persisted index. Overwrites current in-memory index/docs."""
        target = Path(path)
        if not target.exists():
            return
        try:
            raw = target.read_text(encoding="utf-8")
            payload = json.loads(raw)
            docs = payload.get("docs") or {}
            index = payload.get("index") or {}
            # normalize
            with self._lock:
                self._docs = {str(k): v for k, v in docs.items()}
                new_index: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
                for term, postings in index.items():
                    if isinstance(postings, list):
                        for p in postings:
                            if isinstance(p, list) and len(p) >= 2:
                                new_index[str(term)].append((str(p[0]), int(p[1])))
                self._index = new_index
        except Exception:
            # corrupt index file → skip load to avoid crashing startup
            return

    def rebuild_index_from_docs(self, docs: List[Dict]) -> None:
        """Rebuild the index from a list of compaction-like docs.

        Each doc is expected to have `id`, `summary_full`/`summary_sections`,
        `updated_at` and optional `meta`.
        """
        with self._lock:
            if not self._docs:
                return []
            self._index.clear()
            self._docs.clear()
            for comp in docs:
                doc_id = str(comp.get("id") or comp.get("compaction_id") or comp.get("updated_at") or str(time.time()))
                text = comp.get("summary_full")
                if not text:
                    sections = comp.get("summary_sections") or {}
                    parts: List[str] = []
                    for k, v in sections.items():
                        if isinstance(v, list):
                            parts.extend(v)
                        elif isinstance(v, str):
                            parts.append(v)
                    text = " ".join(parts)
                text = (text or "").strip()
                tokens = _tokenize(text)
                tf = defaultdict(int)
                for t in tokens:
                    tf[t] += 1
                for term, count in tf.items():
                    self._index[term].append((doc_id, count))
                sections = comp.get("summary_sections") if isinstance(comp.get("summary_sections"), dict) else None
                category = _classify_memory(text, sections)
                self._docs[doc_id] = {
                    "text": text,
                    "summary_sections": sections,
                    "category": category,
                    "meta": {
                        **(comp.get("meta") or {}),
                        "safety": _assess_safety(text, sections),
                        "category": category,
                    },
                    "updated_at": comp.get("updated_at") or comp.get("created_at") or time.time(),
                    "token_count": len(tokens),
                }

    def index_compactions(self, compactions: List[Dict], replace: bool = True) -> None:
        """Index a list of compaction records.

        Each compaction should contain at least an `id` and either
        `summary_full` (string) or `summary_sections` (dict of lists).
        """
        with self._lock:
            if replace:
                # full rebuild from provided compactions
                self._index.clear()
                self._docs.clear()
                for comp in compactions:
                    doc_id = str(
                        comp.get("id")
                        or comp.get("compaction_id")
                        or comp.get("updated_at")
                        or str(time.time())
                    )
                    text = comp.get("summary_full")
                    if not text:
                        sections = comp.get("summary_sections") or {}
                        parts: List[str] = []
                        for k, v in sections.items():
                            if isinstance(v, list):
                                parts.extend(v)
                            elif isinstance(v, str):
                                parts.append(v)
                        text = " ".join(parts)
                    text = (text or "").strip()
                    tokens = _tokenize(text)
                    tf = defaultdict(int)
                    for t in tokens:
                        tf[t] += 1
                    for term, count in tf.items():
                        self._index[term].append((doc_id, count))
                    sections = comp.get("summary_sections") if isinstance(comp.get("summary_sections"), dict) else None
                    category = _classify_memory(text, sections)
                    self._docs[doc_id] = {
                        "text": text,
                        "summary_sections": sections,
                        "category": category,
                        "meta": {
                            **(comp.get("meta") or {}),
                            "safety": _assess_safety(text, sections),
                            "category": category,
                        },
                        "updated_at": comp.get("updated_at") or comp.get("created_at") or time.time(),
                        "token_count": len(tokens),
                    }
            else:
                # incremental update: update/insert provided compactions
                for comp in compactions:
                    doc_id = str(
                        comp.get("id")
                        or comp.get("compaction_id")
                        or comp.get("updated_at")
                        or str(time.time())
                    )
                    # prefer summary_full, else join sections
                    text = comp.get("summary_full")
                    if not text:
                        sections = comp.get("summary_sections") or {}
                        parts: List[str] = []
                        for k, v in sections.items():
                            if isinstance(v, list):
                                parts.extend(v)
                            elif isinstance(v, str):
                                parts.append(v)
                        text = " ".join(parts)
                    text = (text or "").strip()

                    tokens = _tokenize(text)
                    tf = defaultdict(int)
                    for t in tokens:
                        tf[t] += 1

                    # remove old postings for this doc (if updating)
                    if doc_id in self._docs:
                        for term in list(self._index.keys()):
                            postings = self._index.get(term)
                            if postings:
                                filtered = [(d, c) for (d, c) in postings if d != doc_id]
                                if filtered:
                                    self._index[term] = filtered
                                else:
                                    # remove empty term bucket
                                    del self._index[term]

                    for term, count in tf.items():
                        self._index[term].append((doc_id, count))

                    sections = comp.get("summary_sections") if isinstance(comp.get("summary_sections"), dict) else None
                    category = _classify_memory(text, sections)
                    self._docs[doc_id] = {
                        "text": text,
                        "summary_sections": sections,
                        "category": category,
                        "meta": {
                            **(comp.get("meta") or {}),
                            "safety": _assess_safety(text, sections),
                            "category": category,
                        },
                        "updated_at": comp.get("updated_at") or comp.get("created_at") or time.time(),
                        "token_count": len(tokens),
                    }

    def query(self, query_text: str, top_k: int = 5) -> List[Dict]:
        """Return top_k matching compactions for the query_text.

        Scoring: sum of term TF in doc, divided by sqrt(token_count), with a small recency boost.
        """
        with self._lock:
            if not self._docs:
                return []
            qtokens = _tokenize(query_text)
            scores: Dict[str, float] = defaultdict(float)

            # BM25-like scoring parameters
            N = max(1, len(self._docs))
            avgdl = max(1.0, statistics.mean([d.get("token_count", 1) for d in self._docs.values()]))
            k1 = 1.2
            b = 0.75

            for t in qtokens:
                postings = list(self._index.get(t, []) if isinstance(self._index.get(t, []), list) else [])
                df = max(1, len(postings))
                idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)
                for doc_id, tf in postings:
                    doc = self._docs.get(doc_id)
                    if not doc:
                        continue
                    dl = max(1.0, doc.get("token_count", 1))
                    denom = tf + k1 * (1 - b + b * (dl / avgdl))
                    score_term = idf * ((tf * (k1 + 1)) / denom)
                    scores[doc_id] += score_term

            results: List[Tuple[str, float]] = []
            qtext = query_text.lower().strip()
            for doc_id, score in scores.items():
                doc = self._docs.get(doc_id)
                if not doc:
                    continue
                # recency as timestamp
                raw_recency = doc.get("updated_at", 0)
                recency = 0.0
                if isinstance(raw_recency, (int, float)):
                    recency = float(raw_recency)
                elif isinstance(raw_recency, str):
                    try:
                        recency = float(datetime.fromisoformat(raw_recency).timestamp())
                    except Exception:
                        try:
                            recency = float(raw_recency)
                        except Exception:
                            recency = 0.0
                recency_boost = 1.0 + min(0.2, (recency / (60 * 60 * 24 * 30)) * 0.01) if recency > 0 else 1.0

                # phrase/substring match bonus
                phrase_bonus = 0.0
                if qtext and qtext in (doc.get("text") or "").lower():
                    phrase_bonus = 0.5

                final = (score * recency_boost) + phrase_bonus
                results.append((doc_id, final))

            results.sort(key=lambda x: x[1], reverse=True)
            out: List[Dict] = []
            for doc_id, score in results[:top_k]:
                doc = self._docs[doc_id]
                category = doc.get("category") or (doc.get("meta") or {}).get("category") or "project_fact"
                reason = _match_reason(
                    qtokens,
                    doc.get("text") or "",
                    doc.get("summary_sections") if isinstance(doc.get("summary_sections"), dict) else None,
                    category,
                )
                meta = {
                    **(doc.get("meta") or {}),
                    "category": category,
                    "match_reason": reason,
                }
                out.append(
                    {
                        "id": doc_id,
                        "score": score,
                        "snippet": (doc.get("text") or "")[:300],
                        "meta": meta,
                        "category": category,
                        "match_reason": reason,
                        "updated_at": doc.get("updated_at"),
                    }
                )
            return out


__all__ = ["MemoryRetriever"]
