"""Dev-only HTTP/1.1 response fault injector; logs contain no request content."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import http.client
from pathlib import Path
import re
import socket
import struct
import sys
import tempfile
import time
from urllib.parse import urlsplit

MODES = ("pass", "drop", "truncate", "stall", "oversize", "refuse-large", "redirect")
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_FILLER_BYTES = 64 * 1024 * 1024
STALL_SECONDS = 180
REQUEST_TIMEOUT = 30
HEADER_LIMIT = 65536
TOKEN = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class BadRequest(Exception):
    pass


class TooLarge(Exception):
    pass


def set_fault(directory: Path, mode: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if mode == "clear":
        (directory / "fault").unlink(missing_ok=True)
        return
    if mode not in MODES:
        raise ValueError("invalid mode")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False, encoding="ascii") as output:
            temporary = Path(output.name)
            output.write(mode + "\n")
        temporary.replace(directory / "fault")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def line(reader: asyncio.StreamReader) -> bytes:
    try:
        result = await reader.readuntil(b"\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise BadRequest from None
    if len(result) > HEADER_LIMIT:
        raise BadRequest
    return result[:-2]


async def headers(reader: asyncio.StreamReader) -> dict[str, str]:
    result = {}
    size = 0
    while True:
        raw = await line(reader)
        size += len(raw) + 2
        if size > HEADER_LIMIT:
            raise BadRequest
        if not raw:
            return result
        name, separator, value = raw.partition(b":")
        if not separator or not TOKEN.fullmatch(name) or any(byte < 32 and byte != 9 or byte == 127 for byte in value):
            raise BadRequest
        key = name.decode("ascii").lower()
        if key in result:
            raise BadRequest
        result[key] = value.decode("latin-1").strip()


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    raw = await line(reader)
    parts = raw.split(b" ")
    if len(parts) != 3 or not TOKEN.fullmatch(parts[0]) or parts[2] != b"HTTP/1.1":
        raise BadRequest
    method = parts[0].decode("ascii")
    target = parts[1].decode("ascii")
    if not target.startswith("/") or target.startswith("//") or any(ord(char) <= 32 or ord(char) == 127 for char in target):
        raise BadRequest
    values = await headers(reader)
    transfer = values.get("transfer-encoding")
    length = values.get("content-length")
    if transfer is not None:
        if transfer.lower() != "chunked" or length is not None:
            raise BadRequest
        body = bytearray()
        while True:
            raw_size = (await line(reader)).split(b";", 1)[0]
            if not re.fullmatch(rb"[0-9a-fA-F]+", raw_size):
                raise BadRequest
            size = int(raw_size, 16)
            if len(body) + size > MAX_REQUEST_BYTES:
                raise TooLarge
            if size == 0:
                await headers(reader)  # consume bounded trailers
                return method, target, values, bytes(body)
            body.extend(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise BadRequest
    if length is not None and not re.fullmatch(r"[0-9]+", length):
        raise BadRequest
    size = int(length or 0)
    if size > MAX_REQUEST_BYTES:
        raise TooLarge
    return method, target, values, await reader.readexactly(size)


class FaultProxy:
    def __init__(self, upstream: str, state_dir: Path):
        parsed = urlsplit(upstream)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("invalid upstream")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ordinal = 0
        self.bodies: dict[bytes, tuple[int, int]] = {}
        self.tasks: set[asyncio.Task] = set()
        self.server: asyncio.Server | None = None

    async def start(self, host: str, port: int) -> asyncio.Server:
        self.server = await asyncio.start_server(self.handle, host, port, limit=HEADER_LIMIT)
        return self.server

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def take_fault(self, method: str, target: str) -> str:
        if method != "POST" or urlsplit(target).path != "/v1/outbox-messages":
            return "pass"
        path = self.state_dir / "fault"
        try:
            mode = path.read_text(encoding="ascii").strip()
            path.unlink()
        except FileNotFoundError:
            return "pass"
        return mode if mode in MODES else "pass"

    def forward(self, method: str, target: str, values: dict[str, str], body: bytes) -> tuple[int, str | None, bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            forwarded = {key: values[key] for key in ("content-type", "content-encoding") if key in values}
            forwarded["accept-encoding"] = "identity"
            forwarded["Connection"] = "close"
            connection.request(method, target, body, forwarded)
            response = connection.getresponse()
            return response.status, response.getheader("content-type"), response.read()
        finally:
            connection.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        self.ordinal += 1
        ordinal = self.ordinal
        started = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        method, target, mode, upstream = "UNKNOWN", "/", "pass", "error"
        request_bytes, sent = 0, 0
        body_label = "-"
        ended = "complete"
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)
        writer.transport.set_write_buffer_limits(high=65536, low=0)

        async def send(data: bytes, *, body: bool = False) -> None:
            nonlocal sent
            writer.write(data)
            await writer.drain()
            # drain means the transport accepted the chunk, not that the peer
            # application consumed it. Kernel buffering can include one final
            # chunk at disconnect; never count a chunk whose drain failed.
            if body:
                sent += len(data)

        async def head(status: int, content_type: str | None, length: int | None, location: str | None = None) -> None:
            reason = http.client.responses.get(status, "Response")
            fields = [f"HTTP/1.1 {status} {reason}", "Connection: close"]
            if content_type:
                fields.append(f"Content-Type: {content_type}")
            if length is not None:
                fields.append(f"Content-Length: {length}")
            if location:
                fields.append(f"Location: {location}")
            await send(("\r\n".join(fields) + "\r\n\r\n").encode("latin-1"))

        try:
            try:
                method, target, values, body = await asyncio.wait_for(read_request(reader), REQUEST_TIMEOUT)
            except TooLarge:
                await head(413, None, 0)
                return
            except (BadRequest, ValueError, UnicodeError, asyncio.IncompleteReadError, TimeoutError):
                await head(400, None, 0)
                return
            request_bytes = len(body)
            if method == "POST":
                digest = hashlib.sha256(body).digest()
                body_number, previous = self.bodies.get(digest, (len(self.bodies) + 1, 0))
                attempt = previous + 1
                self.bodies[digest] = (body_number, attempt)
                body_label = f"{body_number}#{attempt}"
            mode = self.take_fault(method, target)
            try:
                status, content_type, response = await asyncio.to_thread(self.forward, method, target, values, body)
                upstream = str(status)
            except (OSError, http.client.HTTPException, ValueError):
                await head(502, None, 0)
                return
            if mode == "drop":
                # abort() alone may send a clean FIN with no queued data.
                # Zero-timeout linger forces a TCP reset on macOS and Linux.
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                ended = "dropped"
                writer.transport.abort()
            elif mode in ("oversize", "refuse-large"):
                await head(200 if mode == "oversize" else 409, "application/json", None)
                chunk = b" " * 65536
                for _ in range(MAX_FILLER_BYTES // len(chunk)):
                    if reader.at_eof():
                        ended = "client-closed"
                        break
                    await send(chunk, body=True)
                    # Give a bounded client time to abort before filling its
                    # receive buffers with the entire synthetic response.
                    await asyncio.sleep(0.001)
            elif mode == "redirect":
                await head(307, "application/json", 65536, target)
                await send(b" " * 65536, body=True)
            else:
                await head(status, content_type, len(response))
                if mode == "truncate":
                    await send(response[:len(response) // 2], body=True)
                elif mode == "stall":
                    await send(response[:1], body=True)
                    deadline = time.monotonic() + STALL_SECONDS
                    try:
                        while await asyncio.wait_for(reader.read(65536), max(0.001, deadline - time.monotonic())):
                            if time.monotonic() >= deadline:
                                raise TimeoutError
                        ended = "client-closed"
                    except TimeoutError:
                        ended = "timeout"
                else:
                    await send(response, body=True)
        except (BrokenPipeError, ConnectionError):
            ended = "client-closed"
        except asyncio.CancelledError:
            ended = "error"
            raise
        except Exception:
            ended = "error"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass
            # Query strings are not diagnostics and may carry secrets.
            path = urlsplit(target).path
            entry = (f"{timestamp} #{ordinal} {method} {path} mode={mode} req_bytes={request_bytes} "
                     f"upstream={upstream} resp_bytes_sent={sent} ended={ended} "
                     f"ms={int((time.monotonic() - started) * 1000)} body={body_label}")
            try:
                with (self.state_dir / "requests.log").open("a", encoding="utf-8") as output:
                    output.write(entry + "\n")
                print(entry, flush=True)
            finally:
                self.tasks.discard(task)


async def serve(args: argparse.Namespace) -> None:
    host, separator, port = args.listen.rpartition(":")
    if not separator or not host or not 1 <= int(port) <= 65535:
        raise ValueError("invalid listen address")
    proxy = FaultProxy(args.upstream, Path(args.state_dir))
    try:
        server = await proxy.start(host.strip("[]"), int(port))
        async with server:
            await server.serve_forever()
    finally:
        await proxy.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("serve")
    run.add_argument("--listen", required=True)
    run.add_argument("--upstream", required=True)
    run.add_argument("--state-dir", required=True)
    fault = commands.add_parser("set-fault")
    fault.add_argument("--state-dir", required=True)
    fault.add_argument("mode", choices=(*MODES, "clear"))
    args = parser.parse_args(argv)
    try:
        if args.command == "set-fault":
            set_fault(Path(args.state_dir), args.mode)
        else:
            asyncio.run(serve(args))
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError):
        print("fault proxy: configuration or I/O failure", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
