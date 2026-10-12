"""Write-once staging: write to _tmp/<run_id>/..., then copy to the final key and keep the ETag."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


@dataclass(frozen=True)
class StagedObject:
    uri: str
    etag: str


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"not an s3:// URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


class Staging:
    def __init__(self, s3: S3Client, bucket: str) -> None:
        self.s3 = s3
        self.bucket = bucket

    def uri(self, prefix: str, name: str) -> str:
        return f"s3://{self.bucket}/{prefix}{name}"

    def write(
        self, prefix: str, name: str, body: bytes, run_id: str, content_type: str
    ) -> StagedObject:
        """Stage `body` at <prefix><name>. A crash mid-write leaves only a _tmp object (expired by
        lifecycle), never a partial final object."""
        final_key = f"{prefix}{name}"
        tmp_key = f"_tmp/{run_id}/{final_key}"
        self.s3.put_object(Bucket=self.bucket, Key=tmp_key, Body=body, ContentType=content_type)
        copied = self.s3.copy_object(
            Bucket=self.bucket,
            Key=final_key,
            CopySource={"Bucket": self.bucket, "Key": tmp_key},
            MetadataDirective="COPY",
        )
        self.s3.delete_object(Bucket=self.bucket, Key=tmp_key)
        etag = copied["CopyObjectResult"]["ETag"].strip('"')
        return StagedObject(uri=f"s3://{self.bucket}/{final_key}", etag=etag)

    def read(self, prefix: str, name: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=f"{prefix}{name}")["Body"].read()

    def etag(self, prefix: str, name: str) -> str | None:
        """Current ETag of a staged object, or None if it doesn't exist (resume checks)."""
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=f"{prefix}{name}")
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return str(head["ETag"]).strip('"')
