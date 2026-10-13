"""producer-cli: register processes and upload files to the producer API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import boto3
import botocore.exceptions

from producer_client.client import ApiError, ProducerClient, UploadError

DEFAULT_CONCURRENCY = 8


def _print(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2) + "\n")


def _metadata(pairs: Sequence[str] | None) -> dict[str, str] | None:
    if not pairs:
        return None
    result: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--metadata expects key=value, got {pair!r}")
        result[key] = value
    return result


def external_id_for(path: Path, root: Path, mode: str) -> str | None:
    if mode == "none":
        return None
    if mode == "filename":
        return path.name
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def iter_files(directory: Path) -> list[Path]:
    return sorted(
        p
        for p in directory.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(directory).parts)
    )


def upload_many(
    client: ProducerClient,
    process_id: str,
    files: Iterable[Path],
    *,
    root: Path,
    external_id_mode: str,
    concurrency: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    uploaded: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                client.upload_file,
                process_id,
                path,
                external_id=external_id_for(path, root, external_id_mode),
            ): path
            for path in files
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                ticket = future.result()
            except (ApiError, UploadError, OSError) as exc:
                failed.append({"file": str(path), "error": str(exc)})
                sys.stderr.write(f"FAILED {path}: {exc}\n")
            else:
                uploaded.append(
                    {
                        "file": str(path),
                        "upload_id": ticket["upload_id"],
                        "article_id": ticket["article_id"],
                    }
                )
                sys.stderr.write(f"uploaded {path}\n")
    return uploaded, failed


def _client(args: argparse.Namespace) -> ProducerClient:
    if not args.api_url:
        raise SystemExit("set --api-url or PRODUCER_API_URL")
    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    return ProducerClient(args.api_url, region=args.region, session=session)


def cmd_create_process(args: argparse.Namespace) -> int:
    process = _client(args).create_process(
        args.name, args.source, args.expected_count, _metadata(args.metadata)
    )
    _print(process)
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    files = [Path(f) for f in args.files]
    uploaded, failed = upload_many(
        _client(args),
        args.process_id,
        files,
        root=Path.cwd(),
        external_id_mode=args.external_id,
        concurrency=args.concurrency,
    )
    _print({"uploaded": uploaded, "failed": failed})
    return 1 if failed else 0


def cmd_ingest_dir(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    if not root.is_dir():
        raise SystemExit(f"{root} is not a directory")
    files = iter_files(root)
    client = _client(args)
    process = client.create_process(args.name, args.source, metadata=_metadata(args.metadata))
    pid = process["process_id"]
    sys.stderr.write(f"process {pid}: uploading {len(files)} file(s)\n")
    uploaded, failed = upload_many(
        client,
        pid,
        files,
        root=root,
        external_id_mode=args.external_id,
        concurrency=args.concurrency,
    )
    result: dict[str, Any] = {"process_id": pid, "uploaded": uploaded, "failed": failed}
    if args.close:
        # Close even if some (or all) uploads failed: the process expects every upload that got a
        # ticket; tickets whose file never arrived expire, so the process still settles.
        result["process"] = client.close_process(pid)
    _print(result)
    return 1 if failed else 0


def cmd_status(args: argparse.Namespace) -> int:
    _print(_client(args).get_process(args.process_id))
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    _print(_client(args).close_process(args.process_id, args.expected_count))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="producer-cli", description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("PRODUCER_API_URL"))
    parser.add_argument("--region", default=None, help="defaults to the region in --api-url")
    parser.add_argument("--profile", default=None, help="AWS profile for signing")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_upload_options(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--external-id",
            choices=("path", "filename", "none"),
            default="path",
            help="how to derive external_id (path is relative to the folder)",
        )
        p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)

    p = sub.add_parser("create-process", help="create a process")
    p.add_argument("--name", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--expected-count", type=int)
    p.add_argument("--metadata", nargs="*", metavar="KEY=VALUE")
    p.set_defaults(func=cmd_create_process)

    p = sub.add_parser("upload", help="upload files to an existing process")
    p.add_argument("process_id")
    p.add_argument("files", nargs="+")
    add_upload_options(p)
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("ingest-dir", help="create a process and upload a whole folder")
    p.add_argument("directory")
    p.add_argument("--name", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--metadata", nargs="*", metavar="KEY=VALUE")
    p.add_argument("--close", action="store_true", help="close the process when done")
    add_upload_options(p)
    p.set_defaults(func=cmd_ingest_dir)

    p = sub.add_parser("status", help="show a process")
    p.add_argument("process_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("close", help="close a process")
    p.add_argument("process_id")
    p.add_argument("--expected-count", type=int)
    p.set_defaults(func=cmd_close)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ApiError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    except (
        UploadError,
        OSError,
        ValueError,
        RuntimeError,
        botocore.exceptions.BotoCoreError,
    ) as exc:
        # Missing credentials, unreadable files, a bad region: a message, not a traceback.
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
