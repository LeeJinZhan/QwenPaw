# -*- coding: utf-8 -*-
"""Built-in tool for uploading a local file to Alibaba Cloud OSS."""
from __future__ import annotations

import asyncio
import mimetypes
import os
import uuid
from pathlib import Path
from urllib.parse import quote

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from .file_io import _resolve_file_path


_REQUIRED_OSS_ENV_VARS = (
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_ACCESS_KEY_ID",
    "OSS_ACCESS_KEY_SECRET",
)


async def upload_file_to_oss(
    file_path: str,
    filename: str | None = None,
) -> ToolResponse:
    """Upload a local file to OSS and return a signed download URL.

    Relative file paths resolve from the current agent workspace. When
    Every upload receives a unique OSS object key. ``filename`` controls the
    name presented when the returned signed URL is downloaded.

    Args:
        file_path: Absolute or workspace-relative path to the local file.
        filename: Optional filename presented to download recipients.
    """
    missing = [key for key in _REQUIRED_OSS_ENV_VARS if not os.getenv(key)]
    if missing:
        return _text_response(
            "OSS upload failed: missing configuration " + ", ".join(missing),
        )

    source_path = Path(_resolve_file_path(file_path)).expanduser().resolve()
    if not source_path.is_file():
        return _text_response("OSS upload failed: local file does not exist")

    try:
        url = await asyncio.to_thread(
            _upload_file,
            source_path,
            filename or source_path.name,
        )
    except (TypeError, ValueError) as exc:
        return _text_response(f"OSS upload failed: {exc}")
    except Exception as exc:  # pylint: disable=broad-except
        return _text_response(f"OSS upload failed: {exc}")

    return _text_response(f"OSS upload succeeded: {url}")


def _upload_file(source_path: Path, filename: str) -> str:
    try:
        import oss2
    except ImportError as exc:
        raise RuntimeError("oss2 dependency is unavailable") from exc

    auth = oss2.Auth(
        os.environ["OSS_ACCESS_KEY_ID"],
        os.environ["OSS_ACCESS_KEY_SECRET"],
    )
    bucket = oss2.Bucket(
        auth,
        os.environ["OSS_ENDPOINT"],
        os.environ["OSS_BUCKET"],
    )
    key = _build_object_key(filename)
    download_headers = _build_download_headers(filename)
    bucket.put_object_from_file(key, str(source_path), headers=download_headers)
    return bucket.sign_url(
        "GET",
        key,
        _download_url_expires(),
        params={
            "response-content-type": download_headers["Content-Type"],
            "response-content-disposition": download_headers[
                "Content-Disposition"
            ],
        },
        slash_safe=True,
    )


def _build_object_key(filename: str) -> str:
    prefix = os.getenv("QWENPAW_OSS_UPLOAD_PREFIX", "").strip().strip("/")
    suffix = Path(filename).suffix
    key = f"{uuid.uuid4().hex}{suffix}"
    return f"{prefix}/{key}" if prefix else key


def _build_download_headers(filename: str) -> dict[str, str]:
    suffix = Path(filename).suffix.lower()
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    encoded_filename = quote(filename, safe="")
    fallback = f"download{suffix}" if suffix else "download"
    return {
        "Content-Type": content_type,
        "Content-Disposition": (
            f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{encoded_filename}"
        ),
    }


def _download_url_expires() -> int:
    raw_value = os.getenv("OSS_DOWNLOAD_URL_EXPIRES", "3600")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(
            "OSS_DOWNLOAD_URL_EXPIRES must be a positive integer",
        ) from exc
    if value <= 0:
        raise ValueError("OSS_DOWNLOAD_URL_EXPIRES must be a positive integer")
    return value


def _text_response(text: str) -> ToolResponse:
    return ToolResponse(content=[TextBlock(type="text", text=text)])
