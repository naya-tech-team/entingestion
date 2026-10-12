"""Domain errors, mapped to HTTP status codes by the API layer."""

from __future__ import annotations

from dataclasses import dataclass, field


class ProducerError(Exception):
    status_code = 500


class NotFoundError(ProducerError):
    status_code = 404


class ConflictError(ProducerError):
    status_code = 409


class ForbiddenError(ProducerError):
    status_code = 403


@dataclass(frozen=True)
class FieldError:
    loc: tuple[str | int, ...]
    msg: str


@dataclass
class RequestValidationError(ProducerError):
    message: str
    errors: list[FieldError] = field(default_factory=list)
    status_code = 422

    def __str__(self) -> str:
        return self.message


class MalformedRequestError(RequestValidationError):
    """The request isn't parseable at all (e.g. invalid JSON): 400, not 422."""

    status_code = 400


class PreconditionFailedError(ProducerError):
    status_code = 412
