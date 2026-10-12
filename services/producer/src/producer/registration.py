"""Register uploaded objects as articles (brief §6.2)."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote_plus

import filetype
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from producer.authz import AccessPolicy
from producer.config import Settings
from producer.models import ArticleStatus, UploadStatus
from producer.repository import Item, Repository, updated_shard
from producer.service import UPLOAD_ROW_TTL
from producer.storage import sha256_hex_to_b64
from producer.timeutil import date_of, iso, utcnow

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

FINISHED_UPLOAD_TTL = timedelta(days=30)
# Object tag on raw objects no article uses (rejected, superseded): the bucket expires them.
UNUSED_TAG = "producer-unused"
HASH_CHUNK = 8 * 1024 * 1024

logger = Logger()

SNIFF_BYTES = 8192
TEXT_TYPES = frozenset({"text/plain", "text/html", "text/markdown"})
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# What the magic numbers may say for each declared binary type. A DOCX is a ZIP whose Office
# parts may sit past the first bytes, so "zip" is accepted for it.
SNIFFED_TYPES: dict[str, set[str]] = {DOCX: {DOCX, "application/zip"}}


def _is_text(start: bytes) -> bool:
    if b"\x00" in start:
        return False
    try:
        start.decode("utf-8")
    except UnicodeDecodeError as err:
        # The sample may end inside a multi-byte character; anything earlier is binary.
        return err.start >= len(start) - 3
    return True


class Outcome(StrEnum):
    REGISTERED = "registered"
    DUPLICATE = "duplicate"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"
    IGNORED = "ignored"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    upload_id: str | None = None
    article_id: str | None = None
    process_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    version_id: str | None = None


def object_refs(s3_event: dict[str, Any]) -> list[ObjectRef]:
    """ObjectCreated records of an S3 notification; s3:TestEvent and other events yield none."""
    if s3_event.get("Event") == "s3:TestEvent":
        return []
    refs = []
    for record in s3_event.get("Records") or []:
        if not str(record.get("eventName", "")).startswith("ObjectCreated:"):
            continue
        s3 = record.get("s3") or {}
        obj = s3.get("object") or {}
        refs.append(
            ObjectRef(
                bucket=str((s3.get("bucket") or {}).get("name", "")),
                key=unquote_plus(str(obj.get("key", ""))),
                version_id=obj.get("versionId"),
            )
        )
    return refs


def _version_id(value: Any) -> str | None:
    # S3 reports "null" for objects written while versioning was off.
    return None if value in (None, "", "null") else str(value)


class Registrar:
    def __init__(self, settings: Settings, repo: Repository, s3: S3Client) -> None:
        self.settings = settings
        self.repo = repo
        self.s3 = s3
        self.access = AccessPolicy.from_json(settings.source_access)
        self._key_pattern = re.compile(
            rf"^{re.escape(settings.raw_prefix)}"
            r"(?P<article_id>art_[0-9A-Za-z]+)/(?P<upload_id>upl_[0-9A-HJKMNP-TV-Z]{26})/[^/]+$"
        )

    def register(self, ref: ObjectRef) -> Result:  # noqa: PLR0911 - one return per §6.2 outcome
        # 1. Only our bucket and raw/art_*/upl_*/<filename> keys.
        if ref.bucket != self.settings.raw_bucket:
            return Result(Outcome.IGNORED, reason="foreign_bucket")
        match = self._key_pattern.match(ref.key)
        if not match:
            return Result(Outcome.IGNORED, reason="unrecognised_key")
        upload_id = match["upload_id"]

        # 2. Claim. Unknown uploads are ignored; already-final ones are duplicates.
        if self.repo.get_upload(upload_id) is None:
            return Result(Outcome.IGNORED, upload_id=upload_id, reason="unknown_upload")
        now = utcnow()
        upload = self.repo.claim_upload(upload_id, self._row_ttl(now))
        if upload is None:
            return Result(Outcome.DUPLICATE, upload_id=upload_id)
        base = {
            "upload_id": upload_id,
            "article_id": str(upload["article_id"]),
            "process_id": str(upload["process_id"]),
        }

        # 3-4. Verify, or reject (upload REJECTED + process rejected_count, atomically).
        problem, head = self._verify(ref, upload, match["article_id"])
        if problem is not None:
            done = self.repo.reject_upload(upload, problem, iso(now), self._finished_ttl(now))
            if not done:
                return Result(Outcome.DUPLICATE, **base)
            self._mark_for_expiry(ref, "rejected")
            return Result(Outcome.REJECTED, reason=problem, **base)

        # 5. Apply to the article; an older upload than the current owner (or than a delete)
        #    is superseded.
        if not self.repo.apply_article(
            self._article_fields(ref, upload, head, now), upload.get("metadata")
        ):
            done = self.repo.finish_upload(
                upload,
                UploadStatus.SUPERSEDED,
                iso(now),
                self._finished_ttl(now),
                reason="newer_upload_registered",
            )
            if done:
                self._mark_for_expiry(ref, "superseded")
            outcome = Outcome.SUPERSEDED if done else Outcome.DUPLICATE
            return Result(outcome, reason="newer_upload_registered", **base)

        # 6. Count the article in the process once (a re-delivery finds the marker and skips).
        self.repo.count_membership(base["process_id"], base["article_id"], iso(now))

        # 7. Done.
        if not self.repo.finish_upload(
            upload, UploadStatus.REGISTERED, iso(now), self._finished_ttl(now)
        ):
            return Result(Outcome.DUPLICATE, **base)
        return Result(Outcome.REGISTERED, **base)

    # -- helpers ------------------------------------------------------------

    def _verify(  # noqa: PLR0911 - one return per rejection reason
        self, ref: ObjectRef, upload: Item, key_article_id: str
    ) -> tuple[str | None, dict[str, Any]]:
        """Returns (rejection reason or None, HEAD response)."""
        if ref.key != upload["s3_key"] or key_article_id != upload["article_id"]:
            return "key_mismatch", {}

        head_args: dict[str, Any] = {
            "Bucket": ref.bucket,
            "Key": ref.key,
            "ChecksumMode": "ENABLED",
        }
        if ref.version_id:
            head_args["VersionId"] = ref.version_id
        try:
            head: dict[str, Any] = dict(self.s3.head_object(**head_args))
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return "object_not_found", {}
            raise

        meta_upload_id = (head.get("Metadata") or {}).get("upload-id")
        if meta_upload_id is not None and meta_upload_id != upload["upload_id"]:
            return "upload_id_metadata_mismatch", head

        size = int(head["ContentLength"])
        declared = int(upload["size_bytes"])
        if size > self.settings.max_file_bytes:
            return f"too_large: {size} bytes exceeds {self.settings.max_file_bytes}", head
        if size != declared:
            return f"size_mismatch: declared {declared}, got {size}", head

        expected_b64 = sha256_hex_to_b64(str(upload["sha256"]))
        stored = head.get("ChecksumSHA256")
        composite = head.get("ChecksumType") == "COMPOSITE" or (
            stored is not None and "-" in stored
        )
        if stored is not None and not composite:
            actual_b64 = str(stored)
        else:
            actual_b64 = self._stream_sha256(ref.bucket, ref.key, head.get("VersionId"))
        if actual_b64 != expected_b64:
            return "sha256_mismatch", head
        if not self._looks_like(ref, head.get("VersionId"), str(upload["content_type"])):
            return "content_type_mismatch", head
        return None, head

    def _looks_like(self, ref: ObjectRef, version_id: str | None, declared: str) -> bool:
        """The declared type is only a label the client chose: check the bytes agree. Binary
        types by their magic numbers; text types are UTF-8 with no NUL bytes."""
        args: dict[str, Any] = {
            "Bucket": ref.bucket,
            "Key": ref.key,
            "Range": f"bytes=0-{SNIFF_BYTES - 1}",
        }
        if version_id:
            args["VersionId"] = version_id
        start = self.s3.get_object(**args)["Body"].read()
        if declared in TEXT_TYPES:
            return _is_text(start)
        kind = filetype.guess(start)
        return kind is not None and kind.mime in SNIFFED_TYPES.get(declared, {declared})

    def _stream_sha256(self, bucket: str, key: str, version_id: str | None) -> str:
        args: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if version_id:
            args["VersionId"] = version_id
        digest = hashlib.sha256()
        body = self.s3.get_object(**args)["Body"]
        for chunk in body.iter_chunks(HASH_CHUNK):
            digest.update(chunk)
        return base64.b64encode(digest.digest()).decode()

    def _article_fields(
        self, ref: ObjectRef, upload: Item, head: dict[str, Any], now: datetime
    ) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "article_id": upload["article_id"],
            "process_id": upload["process_id"],
            "first_process_id": upload["process_id"],
            "source": upload["source"],
            "external_id": upload.get("external_id"),
            "filename": upload["filename"],
            "content_type": upload["content_type"],
            "s3_uri": f"s3://{ref.bucket}/{ref.key}",
            "s3_key": ref.key,
            "s3_version_id": _version_id(head.get("VersionId") or ref.version_id),
            "content_sha256": upload["sha256"],
            "size_bytes": int(head["ContentLength"]),
            "upload_id": upload["upload_id"],
            "status": ArticleStatus.STAGED.value,
            "created_at": iso(now),
            "updated_at": iso(now),
            "updated_date": date_of(now),
            "updated_shard": updated_shard(upload["article_id"], date_of(now)),
            "upload_seq": upload.get("upload_seq"),
        }
        # A re-upload without a title keeps the article's existing one.
        if upload.get("title") is not None:
            fields["title"] = upload["title"]
        # The tenant comes from the source's access policy, never from the caller.
        tenant = self.access.tenant_for(upload["source"])
        if tenant is not None:
            fields["tenant_id"] = tenant
        return fields

    def _mark_for_expiry(self, ref: ObjectRef, state: str) -> None:
        """Tag an object no article uses, so the bucket's lifecycle rule expires it. Best
        effort: an untagged object is kept, not lost."""
        args: dict[str, Any] = {
            "Bucket": ref.bucket,
            "Key": ref.key,
            "Tagging": {"TagSet": [{"Key": UNUSED_TAG, "Value": state}]},
        }
        if ref.version_id:
            args["VersionId"] = ref.version_id
        try:
            self.s3.put_object_tagging(**args)
        except ClientError:
            logger.warning("couldn't tag an unused object", extra={"s3_key": ref.key})

    @staticmethod
    def _row_ttl(now: datetime) -> int:
        return int((now + UPLOAD_ROW_TTL).timestamp())

    @staticmethod
    def _finished_ttl(now: datetime) -> int:
        return int((now + FINISHED_UPLOAD_TTL).timestamp())
