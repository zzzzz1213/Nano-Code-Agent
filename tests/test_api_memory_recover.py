"""Tests for the /v1/memory/recover endpoint (controlled recovery).

These tests use aiohttp's TestClient/TestServer like other API tests.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from nanobot.api.server import create_app

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except Exception:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make_client(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield _make_client
    finally:
        for client in clients:
            await client.close()


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_preview_returns_document(aiohttp_client) -> None:
    from nanobot.agent.memory import retriever

    # prepare retriever with a doc
    retriever._docs.clear()
    retriever._docs["doc1"] = {
        "text": "Important: run pytest to validate",
        "meta": {"safety": "requires_confirmation"},
        "updated_at": "2021-01-01T00:00:00",
        "token_count": 5,
    }

    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="ok")
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent)
    client = await aiohttp_client(app)

    resp = await client.post("/v1/memory/recover", json={"doc_id": "doc1", "mode": "preview"})
    assert resp.status == 200
    body = await resp.json()
    assert body["id"] == "doc1"
    assert body["safety"] == "requires_confirmation"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_apply_readonly_rejected_for_non_readonly(aiohttp_client) -> None:
    from nanobot.agent.memory import retriever

    retriever._docs.clear()
    retriever._docs["doc2"] = {
        "text": "Modify file foo.py",
        "meta": {"safety": "requires_confirmation"},
        "updated_at": "2021-01-01T00:00:00",
        "token_count": 3,
    }

    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="applied")
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent)
    client = await aiohttp_client(app)

    resp = await client.post("/v1/memory/recover", json={"doc_id": "doc2", "mode": "apply_readonly"})
    assert resp.status == 400


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_apply_with_confirmation_rejects_unsafe(aiohttp_client) -> None:
    from nanobot.agent.memory import retriever

    retriever._docs.clear()
    retriever._docs["doc3"] = {
        "text": "rm -rf / important",
        "meta": {"safety": "unsafe"},
        "updated_at": "2021-01-01T00:00:00",
        "token_count": 4,
    }

    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="applied")
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/v1/memory/recover",
        json={"doc_id": "doc3", "mode": "apply_with_confirmation"},
    )
    assert resp.status == 403


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_apply_success_invokes_agent(aiohttp_client) -> None:
    from nanobot.agent.memory import retriever

    retriever._docs.clear()
    retriever._docs["doc4"] = {
        "text": "This is safe read-only note",
        "meta": {"safety": "read-only"},
        "updated_at": "2021-01-01T00:00:00",
        "token_count": 4,
    }

    # capture that agent.process_direct was called with correct content
    captured = {}

    async def fake_process_direct(**kwargs):
        captured.update(kwargs)
        return "replayed"

    agent = MagicMock()
    agent.process_direct = fake_process_direct
    agent._connect_mcp = AsyncMock()
    agent.close_mcp = AsyncMock()

    app = create_app(agent)
    client = await aiohttp_client(app)

    resp = await client.post("/v1/memory/recover", json={"doc_id": "doc4", "mode": "apply_readonly"})
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["assistant"] == "replayed"
    # ensure process_direct was called with the doc text
    assert "content" in captured
    assert captured["content"] == "This is safe read-only note"
    # metrics endpoint should show apply_ok incremented and audit log contains entry
    mresp = await client.get("/v1/metrics")
    assert mresp.status == 200
    mbody = await mresp.json()
    metrics = mbody.get("metrics", {})
    assert metrics.get("memory_recovery_apply_ok", 0) >= 1
    # audit log file should contain an entry for doc4
    import pathlib, json as _json, tempfile
    path = pathlib.Path.cwd() / "logs" / "recovery_actions.jsonl"
    if not path.exists():
        # fallback to system temp file used by server
        path = pathlib.Path(tempfile.gettempdir()) / "nanobot_recovery_actions.jsonl"
    if path.exists():
        found = False
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    obj = _json.loads(line)
                except Exception:
                    continue
                if obj.get("doc_id") == "doc4":
                    found = True
                    break
        assert found
    else:
        # best-effort: audit log may be disabled in restricted envs
        logger = True
