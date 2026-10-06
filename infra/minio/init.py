"""Idempotent MinIO bootstrap (replaces the mc-based script; the mc image/binary is no longer
published): buckets, versioning, SSE-S3, lifecycle, object-lock bucket, service users/policies."""

import json
import os
import pathlib
import sys

from minio import Minio
from minio.commonconfig import ENABLED, Filter
from minio.credentials.providers import StaticProvider
from minio.error import MinioAdminException
from minio.lifecycleconfig import LifecycleConfig, NoncurrentVersionExpiration, Rule
from minio.minioadmin import MinioAdmin
from minio.objectlockconfig import COMPLIANCE, DAYS, ObjectLockConfig
from minio.sseconfig import Rule as SseRule
from minio.sseconfig import SSEConfig
from minio.versioningconfig import VersioningConfig

ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
ROOT = (os.environ["SHARED_MINIO_ROOT_USER"], os.environ["SHARED_MINIO_ROOT_PASSWORD"])
POLICIES = pathlib.Path(__file__).parent / "policies"

client = Minio(ENDPOINT, *ROOT, secure=False)
admin = MinioAdmin(endpoint=ENDPOINT, credentials=StaticProvider(*ROOT), secure=False)

for b in ("hospital-docs", "insurer-docs", "kb-sources"):
    if not client.bucket_exists(b):
        client.make_bucket(b)
    client.set_bucket_versioning(b, VersioningConfig(ENABLED))
    client.set_bucket_encryption(b, SSEConfig(SseRule.new_sse_s3_rule()))
    if b != "kb-sources":
        client.set_bucket_lifecycle(
            b,
            LifecycleConfig(
                [
                    Rule(
                        ENABLED,
                        rule_filter=Filter(prefix=""),
                        rule_id="noncurrent-30d",
                        noncurrent_version_expiration=NoncurrentVersionExpiration(
                            noncurrent_days=30
                        ),
                    )
                ]
            ),
        )

if not client.bucket_exists("audit-anchors"):
    client.make_bucket("audit-anchors", object_lock=True)
client.set_object_lock_config("audit-anchors", ObjectLockConfig(COMPLIANCE, 365, DAYS))
client.set_bucket_encryption("audit-anchors", SSEConfig(SseRule.new_sse_s3_rule()))

USERS = [  # (user, env var with secret, policy file, policy name)
    ("hosp_svc", "HOSP_MINIO_SECRET", "hosp-rw.json", "hosp-rw"),
    ("ins_svc", "INS_MINIO_SECRET", "ins-rw.json", "ins-rw"),
    ("docpipe_hosp", "DOCPIPE_HOSP_MINIO_SECRET", "docpipe-ro-hosp.json", "docpipe-hosp"),
    ("docpipe_ins", "DOCPIPE_INS_MINIO_SECRET", "docpipe-ro-ins.json", "docpipe-ins"),
    ("vision_svc", "VISION_MINIO_SECRET", "vision-ro.json", "vision"),
    ("rag_svc", "RAG_MINIO_SECRET", "rag-ro.json", "rag"),
    ("anchor_svc", "ANCHOR_MINIO_SECRET", "anchor-w.json", "anchor"),
]
for user, env, pfile, pname in USERS:
    json.loads((POLICIES / pfile).read_text())  # validate JSON before sending
    admin.policy_add(pname, str(POLICIES / pfile))
    admin.user_add(user, os.environ[env])
    try:
        admin.attach_policy([pname], user=user)
    except MinioAdminException as e:
        if "AlreadyApplied" not in str(e):  # re-runs are expected
            raise
print("minio-init done", file=sys.stderr)
