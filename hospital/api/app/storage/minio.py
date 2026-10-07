"""Object storage (MinIO via the minio SDK; blocking calls run in worker threads)."""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any, cast

import anyio
from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error


class ObjectStore:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        client: Any = None,
    ) -> None:
        self.bucket = bucket
        self.c = client or Minio(endpoint, access_key, secret_key, secure=secure)

    async def put(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
    ) -> None:
        await anyio.to_thread.run_sync(
            lambda: self.c.put_object(
                self.bucket,
                key,
                io.BytesIO(data),
                len(data),
                content_type=content_type,
                metadata=cast(Any, metadata),
            )
        )

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            r = self.c.get_object(self.bucket, key)
            try:
                return r.read()
            finally:
                r.close()
                r.release_conn()

        return await anyio.to_thread.run_sync(_get)

    async def exists(self, key: str) -> bool:
        def _stat() -> bool:
            try:
                self.c.stat_object(self.bucket, key)
                return True
            except S3Error as e:
                if e.code in ("NoSuchKey", "NoSuchObject"):
                    return False
                raise

        return await anyio.to_thread.run_sync(_stat)

    async def copy(self, src: str, dst: str) -> None:
        await anyio.to_thread.run_sync(
            lambda: self.c.copy_object(self.bucket, dst, CopySource(self.bucket, src))
        )

    async def delete(self, key: str) -> None:
        await anyio.to_thread.run_sync(lambda: self.c.remove_object(self.bucket, key))

    async def delete_quietly(self, key: str) -> None:
        try:
            await self.delete(key)
        except S3Error:
            pass

    async def move(self, src: str, dst: str) -> None:
        await self.copy(src, dst)
        await self.delete(src)

    async def presign_get(self, key: str, ttl_s: int = 300, filename: str | None = None) -> str:
        extra = (
            {"response-content-disposition": f"attachment; filename*=UTF-8''{filename}"}
            if filename
            else None
        )
        return await anyio.to_thread.run_sync(
            lambda: self.c.presigned_get_object(
                self.bucket, key, expires=timedelta(seconds=ttl_s), response_headers=cast(Any, extra)
            )
        )

    async def list_keys(self, prefix: str = "") -> list[str]:
        return await anyio.to_thread.run_sync(
            lambda: [
                o.object_name
                for o in self.c.list_objects(self.bucket, prefix=prefix, recursive=True)
            ]
        )

    async def healthy(self) -> bool:
        try:
            return bool(await anyio.to_thread.run_sync(lambda: self.c.bucket_exists(self.bucket)))
        except Exception:  # noqa: BLE001
            return False
