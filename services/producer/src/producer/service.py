"""Business logic behind the API: processes, upload tickets, articles and who may touch them."""

from __future__ import annotations

from collections.abc import Callable, Set
from datetime import timedelta
from typing import Any

from entcommon.authz import AccessPolicy
from entcommon.paging import TokenError, decode_token, encode_token

from producer.config import Settings
from producer.errors import (
    ConflictError,
    FieldError,
    ForbiddenError,
    NotFoundError,
    PreconditionFailedError,
    RequestValidationError,
)
from producer.ids import article_id_for, new_process_id, new_upload_id
from producer.models import (
    Access,
    Article,
    ArticlePage,
    CloseProcessRequest,
    CreateProcessRequest,
    CreateUploadRequest,
    ListArticlesQuery,
    ListProcessesQuery,
    ListUploadsQuery,
    PatchArticleRequest,
    Process,
    ProcessPage,
    ProcessStatus,
    SourceGrant,
    SourceList,
    Upload,
    UploadPage,
    UploadStatus,
    UploadSummary,
    UploadTicket,
)
from producer.repository import (
    MEMBER_PAGE_KEYS,
    PENDING,
    SOURCE_PAGE_KEYS,
    UPLOAD_PAGE_KEYS,
    Repository,
)
from producer.storage import Storage
from producer.timeutil import date_of, iso, utcnow


def raw_key(prefix: str, article_id: str, upload_id: str, filename: str) -> str:
    return f"{prefix}{article_id}/{upload_id}/{filename}"


class ProducerService:
    def __init__(self, settings: Settings, repo: Repository, storage: Storage) -> None:
        self.settings = settings
        self.repo = repo
        self.storage = storage
        self.access = AccessPolicy.from_json(settings.source_access)

    # -- access ---------------------------------------------------------------

    def _can_read(self, caller: str, item: dict[str, Any], what: str) -> None:
        """Records of a source the caller may not read don't exist, as far as it can tell."""
        if not self.access.can_read(caller, str(item.get("source", ""))):
            raise NotFoundError(f"{what} not found")

    def _can_write(self, caller: str, item: dict[str, Any], what: str) -> None:
        self._can_read(caller, item, what)
        if not self.access.can_write(caller, str(item.get("source", ""))):
            raise ForbiddenError(f"not allowed to change {what} (source {item.get('source')})")

    def list_sources(self, caller: str) -> SourceList:
        """The configured sources the caller may read or write (admins: all of them, as write)."""
        return SourceList(
            items=[
                SourceGrant(source=source, tenant_id=cfg.tenant_id, access=Access(level))
                for source, cfg in sorted(self.access.sources.items())
                if (level := self.access.level(caller, source)) is not None
            ]
        )

    # -- processes ----------------------------------------------------------

    def create_process(self, req: CreateProcessRequest, caller: str) -> Process:
        if not self.access.can_write(caller, req.source):
            raise ForbiddenError(f"not allowed to write source {req.source!r}")
        now = iso(utcnow())
        item: dict[str, Any] = {
            "process_id": new_process_id(),
            "name": req.name,
            "source": req.source,
            "status": ProcessStatus.OPEN.value,
            "registered_count": 0,
            "rejected_count": 0,
            "tickets_issued": 0,
            "pending_uploads": 0,
            "metadata": req.metadata,
            "created_by": caller,
            "created_at": now,
            "updated_at": now,
        }
        if req.expected_count is not None:
            item["expected_count"] = req.expected_count
        self.repo.put_process(item)
        return Process.model_validate(item)

    def _process(self, process_id: str) -> dict[str, Any]:
        item = self.repo.get_process(process_id)
        if item is None:
            raise NotFoundError(f"process {process_id} not found")
        return item

    def get_process(self, process_id: str, caller: str) -> Process:
        item = self._process(process_id)
        self._can_read(caller, item, f"process {process_id}")
        return Process.model_validate(item)

    def close_process(self, process_id: str, req: CloseProcessRequest, caller: str) -> Process:
        row = self._process(process_id)
        self._can_write(caller, row, f"process {process_id}")
        expected = req.expected_count
        if expected is None:
            expected = int(row.get("tickets_issued") or 0)  # every upload requested so far
        item = self.repo.close_process(process_id, expected, iso(utcnow()))
        if item is None:
            # A repeated close (a client retrying after a lost response) gets the same answer.
            current = self._process(process_id)
            if current.get("status") == ProcessStatus.CLOSED and (
                req.expected_count is None or req.expected_count == current.get("expected_count")
            ):
                return Process.model_validate(current)
            raise ConflictError(f"process {process_id} is not OPEN")
        return Process.model_validate(item)

    def list_processes(self, query: ListProcessesQuery, caller: str) -> ProcessPage:
        if not self.access.can_read(caller, query.source):
            raise ForbiddenError(f"not allowed to read source {query.source!r}")
        start_key = _start_key(
            query.next_token,
            SOURCE_PAGE_KEYS,
            lambda key: key["source"] == query.source,
            "next_token belongs to another source",
        )
        items, last_key = self.repo.list_processes_by_source(query.source, query.limit, start_key)
        return ProcessPage(
            items=[Process.model_validate(i) for i in items],
            next_token=encode_token(last_key),
        )

    # -- uploads ------------------------------------------------------------

    def create_upload(self, process_id: str, req: CreateUploadRequest, caller: str) -> UploadTicket:
        row = self._process(process_id)
        self._can_write(caller, row, f"process {process_id}")
        process = Process.model_validate(row)
        if process.status != ProcessStatus.OPEN:
            raise ConflictError(f"process {process_id} is not OPEN")
        self._validate_file(req)

        now = utcnow()
        ttl = self.settings.upload_url_ttl_seconds
        article_id = article_id_for(process.source, req.external_id)
        upload_id = new_upload_id()
        key = raw_key(self.settings.raw_prefix, article_id, upload_id, req.filename)

        item: dict[str, Any] = {
            "upload_id": upload_id,
            "article_id": article_id,
            "process_id": process_id,
            "source": process.source,
            "s3_key": key,
            "filename": req.filename,
            "content_type": req.content_type,
            "size_bytes": req.size_bytes,
            "sha256": req.sha256,
            "status": UploadStatus.PENDING.value,
            "requested_by": caller,
            "created_at": iso(now),
            "expires_at_epoch": int((now + self.settings.upload_row_ttl).timestamp()),
            # Counted in the process's pending_uploads until it has an outcome; listed by the
            # janitor (gsi_pending) once the ticket has expired.
            "pending": PENDING,
            "ticket_expires_epoch": int((now + timedelta(seconds=ttl)).timestamp()),
            "upload_seq": self.repo.next_upload_sequence(article_id),
        }
        for optional in ("external_id", "title", "metadata"):
            value = getattr(req, optional)
            if value is not None:
                item[optional] = value
        if not self.repo.put_pending_upload(item):
            raise ConflictError(f"process {process_id} is not OPEN")

        presigned = self.storage.presign_put(
            key=key,
            content_type=req.content_type,
            sha256_hex=req.sha256,
            upload_id=upload_id,
            expires_in=ttl,
            size_bytes=req.size_bytes,
        )
        return UploadTicket(
            upload_id=upload_id,
            article_id=article_id,
            process_id=process_id,
            url=presigned.url,
            headers=presigned.headers,
            expires_at=iso(now + timedelta(seconds=ttl)),
        )

    def _validate_file(self, req: CreateUploadRequest) -> None:
        errors: list[FieldError] = []
        if req.content_type not in self.settings.allowed_content_types:
            allowed = ", ".join(sorted(self.settings.allowed_content_types))
            errors.append(
                FieldError(("body", "content_type"), f"content_type must be one of: {allowed}")
            )
        if req.size_bytes > self.settings.max_file_bytes:
            errors.append(
                FieldError(
                    ("body", "size_bytes"),
                    f"size_bytes must be at most {self.settings.max_file_bytes}",
                )
            )
        if errors:
            raise RequestValidationError("Request validation failed", errors)

    def list_uploads(self, process_id: str, query: ListUploadsQuery, caller: str) -> UploadPage:
        """Every upload of a process, oldest first, whatever its outcome."""
        self.get_process(process_id, caller)
        start_key = _start_key(
            query.next_token,
            UPLOAD_PAGE_KEYS,
            lambda key: key["process_id"] == process_id,
            "next_token belongs to another process",
        )
        items, last_key = self.repo.list_uploads_by_process(process_id, query.limit, start_key)
        return UploadPage(
            items=[UploadSummary.model_validate(i) for i in items],
            next_token=encode_token(last_key),
        )

    def get_upload(self, upload_id: str, caller: str) -> Upload:
        item = self.repo.get_upload(upload_id)
        if item is None or item.get("kind") == "membership":
            raise NotFoundError(f"upload {upload_id} not found")
        self._can_read(caller, item, f"upload {upload_id}")
        return Upload.model_validate(item)

    # -- articles -----------------------------------------------------------

    def _article(self, article_id: str) -> dict[str, Any]:
        item = self.repo.get_article(article_id)
        if item is None:
            raise NotFoundError(f"article {article_id} not found")
        return item

    def get_article(self, article_id: str, caller: str) -> Article:
        item = self._article(article_id)
        self._can_read(caller, item, f"article {article_id}")
        return Article.model_validate(item)

    def list_articles(self, process_id: str, query: ListArticlesQuery, caller: str) -> ArticlePage:
        self.get_process(process_id, caller)
        start_key = _start_key(
            query.next_token,
            MEMBER_PAGE_KEYS,
            lambda key: key["member_process_id"] == process_id,
            "next_token belongs to another process",
        )
        items, last_key = self.repo.list_articles_by_process(process_id, query.limit, start_key)
        return ArticlePage(
            items=[Article.model_validate(i) for i in items],
            next_token=encode_token(last_key),
        )

    def patch_article(
        self,
        article_id: str,
        req: PatchArticleRequest,
        caller: str,
        *,
        if_unchanged_since: str | None = None,
    ) -> Article:
        self._can_write(caller, self._article(article_id), f"article {article_id}")
        fields = {name: getattr(req, name) for name in req.model_fields_set}
        now = utcnow()
        item = self.repo.patch_article(
            article_id, fields, iso(now), date_of(now), if_unchanged_since=if_unchanged_since
        )
        if item is None:
            if if_unchanged_since is not None and self.repo.get_article(article_id) is not None:
                raise PreconditionFailedError(f"article {article_id} changed since that ETag")
            raise NotFoundError(f"article {article_id} not found")
        return Article.model_validate(item)

    def delete_article(self, article_id: str, caller: str, *, purge: bool = False) -> None:
        """Delete the article (ingestion removes its vectors). purge also erases every stored
        version of its raw files; without it they expire with the bucket's lifecycle."""
        item = self._article(article_id)
        self._can_write(caller, item, f"article {article_id}")
        if not self.repo.delete_article(item, iso(utcnow())):
            raise ConflictError(f"article {article_id} changed meanwhile; try again")
        if purge:
            self.storage.purge(f"{self.settings.raw_prefix}{article_id}/")


def _start_key(
    token: str | None,
    keys: Set[str],
    belongs: Callable[[dict[str, Any]], bool],
    mismatch: str,
) -> dict[str, Any] | None:
    """The ExclusiveStartKey in a listing's next_token: the listing's exact key shape, and bound
    to the query it came from. Anything else is a 422 on query.next_token."""
    if not token:
        return None
    try:
        return decode_token(
            token, expect_keys=keys, require_keys=keys, check=belongs, mismatch=mismatch
        )
    except TokenError as exc:
        raise RequestValidationError(
            "Request validation failed", [FieldError(("query", "next_token"), str(exc))]
        ) from exc
