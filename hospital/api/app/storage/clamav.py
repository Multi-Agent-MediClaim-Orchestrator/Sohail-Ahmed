"""Async clamd INSTREAM client. Fails closed: any connectivity problem raises ScanUnavailable."""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from app.core.errors import ApiError


@dataclass
class ScanResult:
    clean: bool
    signature: str | None = None
    encrypted: bool = False


class ClamAV:
    def __init__(self, host: str, port: int, timeout: float = 30.0, chunk: int = 64 * 1024) -> None:
        self.host, self.port, self.timeout, self.chunk = host, port, timeout, chunk

    async def scan(self, data: bytes) -> ScanResult:
        try:
            return await asyncio.wait_for(self._scan(data), self.timeout)
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            raise ApiError(
                "scan_unavailable", "virus scanner unavailable; try again shortly"
            ) from None

    async def _scan(self, data: bytes) -> ScanResult:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            writer.write(b"zINSTREAM\0")
            for i in range(0, len(data), self.chunk):
                part = data[i : i + self.chunk]
                writer.write(struct.pack(">I", len(part)) + part)
            writer.write(struct.pack(">I", 0))
            await writer.drain()
            reply = (await reader.readuntil(b"\0")).rstrip(b"\0").decode(errors="replace")
        finally:
            writer.close()
        body = reply.split(":", 1)[-1].strip()
        if body == "OK":
            return ScanResult(True)
        if body.endswith("FOUND"):
            sig = body[: -len("FOUND")].strip()
            return ScanResult(False, sig, encrypted="Encrypted" in sig)
        raise ApiError("scan_unavailable", f"scanner error: {body[:80]}")

    async def ping(self) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), 3
            )
            writer.write(b"zPING\0")
            await writer.drain()
            ok = (await reader.readuntil(b"\0")).startswith(b"PONG")
            writer.close()
            return ok
        except (OSError, TimeoutError):
            return False
