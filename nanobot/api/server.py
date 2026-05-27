"""OpenAI-compatible HTTP API server for a fixed nanobot session.

Provides /v1/chat/completions and /v1/models endpoints.
All requests route to a single persistent API session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import time
import uuid
from typing import Any

from aiohttp import web
from loguru import logger
from pathlib import Path

from nanobot.config.paths import get_media_dir
from nanobot.utils.helpers import safe_filename
from nanobot.utils.media_decode import (
    MAX_FILE_SIZE,
)
from nanobot.utils.media_decode import (
    FileSizeExceeded as _FileSizeExceeded,
)
from nanobot.utils.media_decode import (
    save_base64_data_url as _save_base64_data_url,
)
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

__all__ = (
    "MAX_FILE_SIZE",
    "_FileSizeExceeded",
    "_save_base64_data_url",
    "create_app",
    "handle_chat_completions",
)


API_SESSION_KEY = "api:default"
API_CHAT_ID = "default"


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _chat_completion_response(content: str, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _response_text(value: Any) -> str:
    """Normalize process_direct output to plain assistant text."""
    if value is None:
        return ""
    if hasattr(value, "content"):
        return str(getattr(value, "content") or "")
    return str(value)

# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_chunk(delta: str, model: str, chunk_id: str, finish_reason: str | None = None) -> bytes:
    """Format a single OpenAI-compatible SSE chunk."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    return f"data: {_json.dumps(payload)}\n\n".encode()


_SSE_DONE = b"data: [DONE]\n\n"

# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------


def _parse_json_content(body: dict) -> tuple[str, list[str]]:
    """Parse JSON request body. Returns (text, media_paths)."""
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        raise ValueError("Only a single user message is supported")
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        raise ValueError("Only a single user message is supported")

    user_content = message.get("content", "")
    media_dir = get_media_dir("api")
    media_paths: list[str] = []

    if isinstance(user_content, list):
        text_parts: list[str] = []
        for part in user_content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    saved = _save_base64_data_url(url, media_dir)
                    if saved:
                        media_paths.append(saved)
                elif url:
                    raise ValueError(
                        "Remote image URLs are not supported. "
                        "Use base64 data URLs or upload files via multipart/form-data."
                    )
        text = " ".join(text_parts)
    elif isinstance(user_content, str):
        text = user_content
    else:
        raise ValueError("Invalid content format")

    return text, media_paths


async def _parse_multipart(request: web.Request) -> tuple[str, list[str], str | None, str | None]:
    """Parse multipart/form-data. Returns (text, media_paths, session_id, model)."""
    media_dir = get_media_dir("api")
    reader = await request.multipart()
    text = ""
    session_id = None
    model = None
    media_paths: list[str] = []

    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "message":
            text = (await part.read()).decode("utf-8")
        elif part.name == "session_id":
            session_id = (await part.read()).decode("utf-8").strip()
        elif part.name == "model":
            model = (await part.read()).decode("utf-8").strip()
        elif part.name == "files":
            raw = await part.read()
            if len(raw) > MAX_FILE_SIZE:
                raise _FileSizeExceeded(
                    f"File '{part.filename}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB limit"
                )
            base = safe_filename(part.filename or "upload.bin")
            filename = f"{uuid.uuid4().hex[:12]}_{base}"
            dest = media_dir / filename
            dest.write_bytes(raw)
            media_paths.append(str(dest))

    if not text:
        text = "请分析上传的文件"

    return text, media_paths, session_id, model


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def handle_chat_completions(request: web.Request) -> web.Response:
    """POST /v1/chat/completions — supports JSON and multipart/form-data."""
    content_type = request.content_type or ""
    if not isinstance(content_type, str):
        content_type = ""

    agent_loop = request.app["agent_loop"]
    timeout_s: float = request.app.get("request_timeout", 120.0)
    model_name: str = request.app.get("model_name", "nanobot")

    stream = False
    try:
        if content_type.startswith("multipart/"):
            text, media_paths, session_id, requested_model = await _parse_multipart(request)
        else:
            try:
                body = await request.json()
            except Exception:
                return _error_json(400, "Invalid JSON body")
            stream = body.get("stream", False)
            requested_model = body.get("model")
            text, media_paths = _parse_json_content(body)
            session_id = body.get("session_id")
    except ValueError as e:
        return _error_json(400, str(e))
    except _FileSizeExceeded as e:
        return _error_json(413, str(e), err_type="invalid_request_error")
    except Exception:
        logger.exception("Error parsing upload")
        return _error_json(413, "File too large or invalid upload")

    if requested_model and requested_model != model_name:
        return _error_json(400, f"Only configured model '{model_name}' is available")

    session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
    session_locks: dict[str, asyncio.Lock] = request.app["session_locks"]
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    logger.info(
        "API request session_key={} media={} text={} stream={}",
        session_key, len(media_paths), text[:80], stream,
    )
    # -- streaming path --
    if stream:
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stream_failed = False
        emitted_content = False

        async def _on_stream(token: str) -> None:
            nonlocal emitted_content
            if token:
                emitted_content = True
            await queue.put(token)

        async def _on_stream_end(*_a: Any, **_kw: Any) -> None:
            # Agent stream-end callbacks mark generation segment boundaries.
            # Tool-backed requests may continue after a segment ends, so the
            # HTTP SSE stream is closed only when process_direct returns.
            return None

        async def _run() -> None:
            nonlocal stream_failed
            try:
                async with session_lock:
                    response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                            on_stream=_on_stream,
                            on_stream_end=_on_stream_end,
                        ),
                        timeout=timeout_s,
                    )
                    if not emitted_content:
                        response_text = _response_text(response)
                        if response_text.strip():
                            await queue.put(response_text)
            except Exception:
                stream_failed = True
                logger.exception("Streaming error for session {}", session_key)
            finally:
                await queue.put(None)

        task = asyncio.create_task(_run())
        try:
            while True:
                token = await queue.get()
                if token is None:
                    break
                await resp.write(_sse_chunk(token, model_name, chunk_id))
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if not stream_failed:
            await resp.write(_sse_chunk("", model_name, chunk_id, finish_reason="stop"))
            await resp.write(_SSE_DONE)
        return resp

    # -- non-streaming path (original logic) --
    fallback = EMPTY_FINAL_RESPONSE_MESSAGE

    try:
        async with session_lock:
            try:
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=text,
                        media=media_paths if media_paths else None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                    ),
                    timeout=timeout_s,
                )
                response_text = _response_text(response)

                if not response_text or not response_text.strip():
                    logger.warning("Empty response for session {}, retrying", session_key)
                    retry_response = await asyncio.wait_for(
                        agent_loop.process_direct(
                            content=text,
                            media=media_paths if media_paths else None,
                            session_key=session_key,
                            channel="api",
                            chat_id=API_CHAT_ID,
                        ),
                        timeout=timeout_s,
                    )
                    response_text = _response_text(retry_response)
                    if not response_text or not response_text.strip():
                        logger.warning("Empty response after retry, using fallback")
                        response_text = fallback

            except asyncio.TimeoutError:
                return _error_json(504, f"Request timed out after {timeout_s}s")
            except Exception:
                logger.exception("Error processing request for session {}", session_key)
                return _error_json(500, "Internal server error", err_type="server_error")
    except Exception:
        logger.exception("Unexpected API lock error for session {}", session_key)
        return _error_json(500, "Internal server error", err_type="server_error")

    return web.json_response(_chat_completion_response(response_text, model_name))


async def handle_models(request: web.Request) -> web.Response:
    """GET /v1/models"""
    model_name = request.app.get("model_name", "nanobot")
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": model_name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "nanobot",
                }
            ],
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    """GET /health"""
    return web.json_response({"status": "ok"})


async def handle_memory_recover(request: web.Request) -> web.Response:
    """POST /v1/memory/recover — controlled recovery of a retrieved memory.

    JSON body: {"doc_id": str, "mode": "preview"|"apply_readonly"|"apply_with_confirmation", "session_id": str (optional)}
    """
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")

    doc_id = body.get("doc_id")
    mode = body.get("mode", "preview")
    session_id = body.get("session_id")
    if not doc_id:
        return _error_json(400, "Missing doc_id")

    # module-level retriever instance maintained by MemoryStore
    try:
        from nanobot.agent.memory import retriever
    except Exception:
        return _error_json(500, "Retriever not available", err_type="server_error")

    with contextlib.suppress(Exception):
        # access internal docs map; defensive copy
        doc = (retriever._docs.get(doc_id) if hasattr(retriever, "_docs") else None)
    if not doc:
        return _error_json(404, f"Document {doc_id} not found")

    safety = (doc.get("meta") or {}).get("safety", "read-only")

    # determine session key early for policy evaluation / metrics
    session_key = f"api:{session_id}" if session_id else API_SESSION_KEY

    if mode == "preview":
        # record preview telemetry
        try:
            request.app["metrics"]["memory_recovery_total"] += 1
            request.app["metrics"]["memory_recovery_preview"] += 1
        except Exception:
            pass
        return web.json_response({
            "id": doc_id,
            "safety": safety,
            "snippet": (doc.get("text") or "")[:200],
            "meta": doc.get("meta") or {},
        })

    # Enforce safety rules for apply modes
    # consult policy engine (if present) before allowing apply
    try:
        policy_engine = request.app.get("policy_engine")
        if policy_engine:
            decision = policy_engine.evaluate(doc, session_key)
            # decision: {decision: 'allow'|'block'|'require_confirmation', ...}
            d = decision.get("decision")
            if d == "block":
                try:
                    request.app["metrics"]["memory_recovery_total"] += 1
                    request.app["metrics"]["memory_recovery_apply_blocked"] += 1
                except Exception:
                    pass
                return _error_json(403, f"Blocked by policy: {decision.get('reason')}")
            if d == "require_confirmation" and mode == "apply_readonly":
                try:
                    request.app["metrics"]["memory_recovery_total"] += 1
                    request.app["metrics"]["memory_recovery_apply_blocked"] += 1
                except Exception:
                    pass
                return _error_json(400, "Document requires confirmation per policy")
    except Exception:
        logger.exception("Policy engine evaluation failed")

    if mode == "apply_readonly" and safety != "read-only":
        try:
            request.app["metrics"]["memory_recovery_total"] += 1
            request.app["metrics"]["memory_recovery_apply_blocked"] += 1
        except Exception:
            pass
        return _error_json(400, "Document not allowed for read-only apply")
    if mode == "apply_with_confirmation" and safety == "unsafe":
        try:
            request.app["metrics"]["memory_recovery_total"] += 1
            request.app["metrics"]["memory_recovery_apply_blocked"] += 1
        except Exception:
            pass
        return _error_json(403, "Document marked unsafe and cannot be auto-applied")

    # perform apply by invoking agent_loop.process_direct with the recovered text
    agent_loop = request.app["agent_loop"]
    timeout_s: float = request.app.get("request_timeout", 120.0)
    session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
    session_locks: dict[str, asyncio.Lock] = request.app["session_locks"]
    session_lock = session_locks.setdefault(session_key, asyncio.Lock())

    # perform apply by invoking agent_loop.process_direct with the recovered text
    try:
        async with session_lock:
            try:
                response = await asyncio.wait_for(
                    agent_loop.process_direct(
                        content=(doc.get("text") or ""),
                        media=None,
                        session_key=session_key,
                        channel="api",
                        chat_id=API_CHAT_ID,
                    ),
                    timeout=timeout_s,
                )
                response_text = _response_text(response)
                # success telemetry
                try:
                    request.app["metrics"]["memory_recovery_total"] += 1
                    request.app["metrics"]["memory_recovery_apply_ok"] += 1
                except Exception:
                    pass
            except asyncio.TimeoutError:
                try:
                    request.app["metrics"]["memory_recovery_total"] += 1
                    request.app["metrics"]["memory_recovery_apply_failed"] += 1
                except Exception:
                    pass
                return _error_json(504, f"Recovery request timed out after {timeout_s}s")
            except Exception:
                logger.exception("Error performing recovery for {}", doc_id)
                try:
                    request.app["metrics"]["memory_recovery_total"] += 1
                    request.app["metrics"]["memory_recovery_apply_failed"] += 1
                except Exception:
                    pass
                return _error_json(500, "Internal server error", err_type="server_error")
    except Exception:
        logger.exception("Unexpected memory recover lock error for {}", doc_id)
        try:
            request.app["metrics"]["memory_recovery_total"] += 1
            request.app["metrics"]["memory_recovery_apply_failed"] += 1
        except Exception:
            pass
        return _error_json(500, "Internal server error", err_type="server_error")

    # audit record (append JSONL) — best-effort
    try:
        path = request.app.get("telemetry_recovery_path")
        if not path:
            import tempfile

            try:
                path = Path(tempfile.gettempdir()) / "nanobot_recovery_actions.jsonl"
            except Exception:
                path = None
        if path:
            entry = {
                "ts": int(time.time()),
                "doc_id": doc_id,
                "mode": mode,
                "safety": safety,
                "session_key": session_key,
                "result": "ok",
            }
            async with request.app["telemetry_lock"]:
                logger.info("Writing recovery telemetry to {}", path)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to write recovery telemetry")

    logger.info("Memory recovered id={} safety={} session={}", doc_id, safety, session_key)
    return web.json_response({"status": "ok", "id": doc_id, "safety": safety, "assistant": response_text})


async def handle_metrics(request: web.Request) -> web.Response:
    """GET /v1/metrics — lightweight JSON metrics for telemetry."""
    metrics = request.app.get("metrics") or {}
    return web.json_response({"metrics": metrics})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    agent_loop, model_name: str = "nanobot", request_timeout: float = 120.0
) -> web.Application:
    """Create the aiohttp application.

    Args:
        agent_loop: An initialized AgentLoop instance.
        model_name: Model name reported in responses.
        request_timeout: Per-request timeout in seconds.
    """
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20MB for base64 images
    app["agent_loop"] = agent_loop
    app["model_name"] = model_name
    app["request_timeout"] = request_timeout
    app["session_locks"] = {}  # per-user locks, keyed by session_key
    # lightweight telemetry: counters and audit log
    app["metrics"] = {
        "memory_recovery_total": 0,
        "memory_recovery_preview": 0,
        "memory_recovery_apply_ok": 0,
        "memory_recovery_apply_blocked": 0,
        "memory_recovery_apply_failed": 0,
    }
    app["telemetry_lock"] = asyncio.Lock()
    # audit log path (project-local logs directory)
    try:
        logs_dir = Path.cwd() / "logs"
        logs_dir.mkdir(exist_ok=True)
        app["telemetry_recovery_path"] = logs_dir / "recovery_actions.jsonl"
    except Exception:
        import tempfile

        try:
            tmp = Path(tempfile.gettempdir())
            app["telemetry_recovery_path"] = tmp / "nanobot_recovery_actions.jsonl"
        except Exception:
            app["telemetry_recovery_path"] = None

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    # Controlled memory recovery endpoint (P2.3)
    app.router.add_post("/v1/memory/recover", handle_memory_recover)
    # Policy endpoints (P3.2 PoC)
    try:
        from nanobot.security.policy import PolicyEngine

        app["policy_engine"] = PolicyEngine()

        async def handle_get_rules(request: web.Request) -> web.Response:
            return web.json_response({"rules": app["policy_engine"].get_rules()})

        async def handle_set_rules(request: web.Request) -> web.Response:
            try:
                body = await request.json()
            except Exception:
                return _error_json(400, "Invalid JSON body")
            rules = body.get("rules") or []
            app["policy_engine"].set_rules(rules)
            return web.json_response({"status": "ok"})

        async def handle_evaluate(request: web.Request) -> web.Response:
            try:
                body = await request.json()
            except Exception:
                return _error_json(400, "Invalid JSON body")
            doc_id = body.get("doc_id")
            if not doc_id:
                return _error_json(400, "Missing doc_id")
            try:
                from nanobot.agent.memory import retriever
            except Exception:
                return _error_json(500, "Retriever not available", err_type="server_error")
            doc = (retriever._docs.get(doc_id) if hasattr(retriever, "_docs") else None)
            if not doc:
                return _error_json(404, f"Document {doc_id} not found")
            session_id = body.get("session_id")
            session_key = f"api:{session_id}" if session_id else API_SESSION_KEY
            decision = app["policy_engine"].evaluate(doc, session_key)
            return web.json_response(decision)

        app.router.add_get("/v1/policy/rules", handle_get_rules)
        app.router.add_post("/v1/policy/rules", handle_set_rules)
        app.router.add_post("/v1/policy/evaluate", handle_evaluate)
    except Exception:
        logger.exception("Failed to initialize PolicyEngine PoC")
    app.router.add_get("/v1/metrics", handle_metrics)
    return app
