"""S3 client configuration and presigned upload URLs."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

# Mandatory: without these, newer botocore adds a CRC32 checksum requirement
# to presigned URLs and plain client PUTs fail.
# addressing_style="virtual" presigns against the regional host (bucket.s3.<region>.amazonaws.com).
# The default uses the global host, which S3 307-redirects to the regional one for new buckets;
# the redirected request's Host no longer matches the signature (SignatureDoesNotMatch).
# us_east_1_regional_endpoint keeps it regional in us-east-1 too, where botocore would otherwise
# use bucket.s3.amazonaws.com: the upload UI's CSP allows only the regional host.
S3_CLIENT_CONFIG = Config(
    signature_version="s3v4",
    request_checksum_calculation="when_required",
    response_checksum_validation="when_required",
    s3={"addressing_style": "virtual", "us_east_1_regional_endpoint": "regional"},
)


def make_s3_client() -> S3Client:
    return boto3.client("s3", config=S3_CLIENT_CONFIG)


def sha256_hex_to_b64(hex_digest: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_digest)).decode()


@dataclass(frozen=True)
class PresignedPut:
    url: str
    headers: dict[str, str]


class Storage:
    def __init__(self, s3: S3Client, bucket: str) -> None:
        self.s3 = s3
        self.bucket = bucket

    def presign_put(
        self,
        *,
        key: str,
        content_type: str,
        sha256_hex: str,
        upload_id: str,
        expires_in: int,
        size_bytes: int,
    ) -> PresignedPut:
        checksum_b64 = sha256_hex_to_b64(sha256_hex)
        url = self.s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": content_type,
                # Signed: S3 refuses a body of any other size (the checksum then pins the bytes).
                "ContentLength": size_bytes,
                "ChecksumSHA256": checksum_b64,
                "Metadata": {"upload-id": upload_id},
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )
        headers = {
            "Content-Type": content_type,
            "x-amz-checksum-sha256": checksum_b64,
            "x-amz-meta-upload-id": upload_id,
        }
        # The signer may hoist some headers into the query string; the client must not send those.
        in_query = {name.lower() for name, _ in parse_qsl(urlsplit(url).query)}
        headers = {k: v for k, v in headers.items() if k.lower() not in in_query}
        return PresignedPut(url=url, headers=headers)

    def purge(self, prefix: str) -> int:
        """Delete every version and delete marker under `prefix` (erasure). Returns the count."""
        deleted = 0
        paginator = self.s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects: list[Any] = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
            ]
            for start in range(0, len(objects), 1000):
                batch = objects[start : start + 1000]
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})
                deleted += len(batch)
        return deleted
