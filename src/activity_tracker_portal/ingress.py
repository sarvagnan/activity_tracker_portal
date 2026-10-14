"""Thin HTTP adapters over the published Portal modules.

All writers, including future CLI intents and accept-* routes, must use
PortalWriter.run for the entire governed call. Its store is exposed for
independent read snapshots only; writing directly bypasses serialization.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path
from typing import TypeVar

import uvicorn
from fastapi import FastAPI, Request, Response

from activity_tracker_portal import ingest_protocol, operational_store, sender_installation
from activity_tracker_portal.operational_store import OperationalStore

T = TypeVar("T")
ENROLLMENT_BODY_LIMIT = 4096
RETRYABLE_BODY = ingest_protocol.encode_message({
    "wire_version": 1, "kind": "retryable-failure", "reason": "temporarily-unavailable",
})


@dataclass(frozen=True)
class PortalSettings:
    """Required JSON settings, with capability fields under ``capabilities``.

    Paths are interpreted relative to the process working directory. No
    policy value (reserve, versions, or limits) has an implicit default.
    """

    store_path: Path
    reserve_bytes: int
    portal_url: str
    host: str
    port: int
    capabilities: sender_installation.Capabilities

    @classmethod
    def from_file(cls, path: str | PathLike[str]) -> PortalSettings:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if type(data) is not dict or set(data) != {
                "store_path", "reserve_bytes", "portal_url", "host", "port", "capabilities",
            }:
                raise ValueError
            if type(data["store_path"]) is not str or not data["store_path"] or "\x00" in data["store_path"]:
                raise ValueError
            if type(data["reserve_bytes"]) is not int or data["reserve_bytes"] < 0:
                raise ValueError
            if type(data["host"]) is not str or not data["host"] or any(char.isspace() or char == "\x00" for char in data["host"]):
                raise ValueError
            if type(data["port"]) is not int or not 1 <= data["port"] <= 65535:
                raise ValueError
            content = data["capabilities"]
            if type(content) is not dict or set(content) != {
                "wire_versions", "projection_schemas", "max_record_count", "max_message_bytes",
            }:
                raise ValueError
            # The published protocol owns URL, schema, version, and limit validity.
            ingest_protocol.encode_message({
                "wire_version": 1, "kind": "enrollment-offer",
                "enrollment_code": "AAAAAAAAAAAAAAAA", "portal_url": data["portal_url"],
                "expires_at": "2000-01-01T00:00:00.000Z",
            })
            ingest_protocol.encode_message({
                "wire_version": 1, "kind": "capability-advertisement", "generation": 1,
                **content,
            })
            capabilities = sender_installation.Capabilities(
                wire_versions=tuple(content["wire_versions"]),
                projection_schemas=tuple(
                    (schema["record_type"], tuple(schema["versions"])) for schema in content["projection_schemas"]
                ),
                max_record_count=content["max_record_count"],
                max_message_bytes=content["max_message_bytes"],
            )
            return cls(Path(data["store_path"]), data["reserve_bytes"], data["portal_url"], data["host"], data["port"], capabilities)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, ingest_protocol.ProtocolRefusal):
            # Never echo config values, paths, parser excerpts, or exception text.
            raise ValueError("invalid Portal configuration") from None


class PortalWriterBusy(RuntimeError):
    """Another Portal or cooperating writing CLI owns this store's writer."""

    failure = "portal-writer-already-running"

    def __init__(self) -> None:
        super().__init__(self.failure)


class PortalWriter:
    """One serialized writer with process exclusion and a thread-owned store.

    SQLite connections are thread-affine. A dedicated thread opens, writes,
    and closes the store, while one lock encloses each complete check/write
    operation and its result. Readers can use store.read() on other threads.
    The advisory lock file stays on disk: unlinking it would split ownership
    between different inodes. Every writing entry point must cooperate.
    """

    def __init__(self, store_path: str | PathLike[str], *, reserve_bytes: int) -> None:
        self._lock = threading.Lock()
        self._closed = False
        path = Path(store_path).resolve()
        self._lock_file = Path(f"{path}.writer-lock").open("a+b")
        try:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PortalWriterBusy() from None
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="portal-writer")
            try:
                self.store = self._executor.submit(
                    operational_store.open_store, path, reserve_bytes=reserve_bytes,
                ).result()
            except BaseException:
                self._executor.shutdown(wait=True)
                raise
        except BaseException:
            self._lock_file.close()
            raise

    def run(self, operation: Callable[[OperationalStore], T]) -> T:
        """Run a whole governed operation, including its checks, without interleaving."""
        with self._lock:
            if self._closed:
                raise RuntimeError("portal-writer-closed")
            return self._executor.submit(operation, self.store).result()

    def close(self) -> None:
        """Wait for any active operation, close on its owning thread, and release flock."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._executor.submit(self.store.close).result()
            finally:
                try:
                    self._executor.shutdown(wait=True)
                finally:
                    self._lock_file.close()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_app(
    settings: PortalSettings,
    *,
    clock: Callable[[], datetime] = _utc_now,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> FastAPI:
    """Create the one-process ingress; future mutation routes share state.writer."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        writer = PortalWriter(settings.store_path, reserve_bytes=settings.reserve_bytes)
        try:
            app.state.writer = writer
            await asyncio.to_thread(writer.run, lambda store: sender_installation.honor_capabilities(store, settings.capabilities, clock()))
            yield
        finally:
            writer.close()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock
    app.state.random_bytes = random_bytes

    @app.post("/v1/enrollment-confirmation")
    async def enrollment_confirmation(request: Request) -> Response:
        # Bound the streamed body before looking up or using the writer.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > ENROLLMENT_BODY_LIMIT:
                return Response(content=b"", status_code=413)
            body.extend(chunk)
        data = bytes(body)
        result = await asyncio.to_thread(
            request.app.state.writer.run,
            lambda store: sender_installation.answer_enrollment_confirmation(store, data, clock()),
        )
        if result.outcome == "refused":
            return Response(content=b"", status_code=409)
        return Response(content=result.body, status_code=200 if result.outcome == "pending" else 503, media_type="application/json")

    @app.get("/v1/capabilities")
    def capabilities(request: Request) -> Response:
        try:
            body = sender_installation.current_advertisement(request.app.state.writer.store)
        except sender_installation.InstallationRefusal as refusal:
            if refusal.failure != "no-capability-generation":
                raise
            return Response(content=RETRYABLE_BODY, status_code=503, media_type="application/json")
        return Response(content=body, status_code=200, media_type="application/json")

    return app


def create_server_config(settings: PortalSettings) -> uvicorn.Config:
    """Build the single-process server without logging private request targets."""
    return uvicorn.Config(
        create_app(settings), host=settings.host, port=settings.port,
        workers=1, reload=False, access_log=False,
    )
