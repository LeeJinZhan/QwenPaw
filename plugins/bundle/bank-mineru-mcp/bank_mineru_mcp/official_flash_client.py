"""Direct no-auth adapter for the official MinerU Flash API."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx

from .config import MinerUSettings
from .mineru_client import MinerUClientError

_REMOTE_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_LANGUAGE = {"auto": "ch", "zh": "ch", "en": "en"}
_FLASH_MAX_BYTES = 10 * 1024 * 1024
_FLASH_MAX_PDF_PAGES = 20
_API_ERROR_CODES = {
    "-30001": "DOCUMENT_TOO_LARGE",
    "-30002": "FILE_TYPE_UNSUPPORTED",
    "-30003": "DOCUMENT_PAGE_LIMIT_EXCEEDED",
    "-30004": "FILE_REF_INVALID",
}


class OfficialFlashMinerUClient:
    """Translate the official signed-upload API into the plugin result contract."""

    def __init__(
        self,
        settings: MinerUSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = http_client or httpx.AsyncClient(
            proxy=settings.proxy_url or None,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.parse_timeout_seconds,
                write=settings.upload_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
            follow_redirects=False,
        )

    async def probe(self) -> dict[str, Any]:
        # The public Flash API has no health endpoint. Configuration is checked
        # at startup and reachability is checked by the first parse request.
        return {"status": "healthy", "provider": "official_flash"}

    async def parse(
        self,
        files: Sequence[Any],
        *,
        parse_method: str = "auto",
        language: str = "auto",
        tables: bool = True,
        formulas: bool = True,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if not 1 <= len(files) <= 5:
            raise MinerUClientError("FILE_REF_INVALID", "MinerU file count is invalid")
        upload_stems = {item.file_id: f"file_{item.file_id}" for item in files}
        results: dict[str, dict[str, Any]] = {}
        for item in files:
            if int(item.size_bytes) > _FLASH_MAX_BYTES:
                raise MinerUClientError(
                    "DOCUMENT_TOO_LARGE",
                    "Official MinerU Flash accepts files up to 10 MB",
                )
            stem = upload_stems[item.file_id]
            page_count = _pdf_page_count(item)
            if page_count is not None and page_count > _FLASH_MAX_PDF_PAGES:
                parts: list[str] = []
                for first_page in range(1, page_count + 1, _FLASH_MAX_PDF_PAGES):
                    last_page = min(
                        first_page + _FLASH_MAX_PDF_PAGES - 1,
                        page_count,
                    )
                    markdown, _ = await self._parse_one(
                        item,
                        upload_name=f"{stem}{item.extension}",
                        parse_method=parse_method,
                        language=language,
                        tables=tables,
                        formulas=formulas,
                        page_range=f"{first_page}-{last_page}",
                    )
                    parts.append(markdown.rstrip())
                markdown = "\n\n".join(parts)
            else:
                markdown, page_count = await self._parse_one(
                    item,
                    upload_name=f"{stem}{item.extension}",
                    parse_method=parse_method,
                    language=language,
                    tables=tables,
                    formulas=formulas,
                )
            results[stem] = {
                "md_content": markdown,
                "page_count": page_count,
            }
        return {"results": results}, upload_stems

    async def close(self) -> None:
        await self._client.aclose()

    async def _parse_one(
        self,
        item: Any,
        *,
        upload_name: str,
        parse_method: str,
        language: str,
        tables: bool,
        formulas: bool,
        page_range: str | None = None,
    ) -> tuple[str, int | None]:
        payload: dict[str, Any] = {
            "file_name": upload_name,
            "language": _LANGUAGE.get(language, language),
            "enable_formula": bool(formulas),
            "enable_table": bool(tables),
        }
        if parse_method == "ocr":
            payload["is_ocr"] = True
        elif parse_method == "txt":
            payload["is_ocr"] = False
        if page_range is not None:
            payload["page_range"] = page_range
        submission = await self._request_api("POST", "/parse/file", json_body=payload)
        task_id = str(submission.get("task_id") or "")
        file_url = self._asset_url(submission.get("file_url"))
        if not _REMOTE_TASK_ID.fullmatch(task_id):
            raise MinerUClientError(
                "MINERU_SUBMIT_AMBIGUOUS",
                "Official MinerU submission response is invalid",
            )
        await self._upload(file_url, Path(item.path).read_bytes())
        completed = await self._wait_for_task(task_id)
        markdown_url = self._asset_url(completed.get("markdown_url"))
        markdown = await self._download_markdown(markdown_url)
        progress = completed.get("extract_progress")
        page_count = None
        if isinstance(progress, dict):
            try:
                page_count = int(progress.get("total_pages"))
            except (TypeError, ValueError):
                page_count = None
        return markdown, page_count

    async def _wait_for_task(self, task_id: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.parse_timeout_seconds
        while True:
            if loop.time() >= deadline:
                raise MinerUClientError(
                    "MINERU_TIMEOUT", "Official MinerU task timed out"
                )
            task = await self._request_api("GET", f"/parse/{task_id}")
            state = str(task.get("state") or "")
            if state == "done":
                if not task.get("markdown_url"):
                    raise MinerUClientError(
                        "MINERU_PARSE_FAILED",
                        "Official MinerU result is missing",
                    )
                return task
            if state == "failed":
                error_code = _API_ERROR_CODES.get(
                    str(task.get("err_code", "")),
                    "MINERU_PARSE_FAILED",
                )
                raise MinerUClientError(
                    error_code,
                    "Official MinerU task failed",
                )
            # Match the official SDK contract: only done and failed are
            # terminal. New server-side transitional state names remain
            # bounded by the local parse deadline.
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _request_api(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                f"{self.settings.base_url}{endpoint}",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=json_body,
                timeout=self.settings.parse_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise MinerUClientError(
                "MINERU_TIMEOUT", "Official MinerU request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise MinerUClientError(
                "MINERU_UNAVAILABLE", "Official MinerU request failed"
            ) from exc
        payload = await self._json_response(response)
        code = str(payload.get("code", "0"))
        if code != "0":
            raise MinerUClientError(
                _API_ERROR_CODES.get(code, "MINERU_PARSE_FAILED"),
                "Official MinerU rejected the request",
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MinerUClientError(
                "MINERU_PARSE_FAILED", "Official MinerU response is invalid"
            )
        return data

    async def _upload(self, url: str, content: bytes) -> None:
        try:
            response = await self._client.put(
                url,
                content=content,
                timeout=self.settings.upload_timeout_seconds,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise MinerUClientError(
                "MINERU_TIMEOUT", "Official MinerU upload timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise MinerUClientError(
                "MINERU_UNAVAILABLE", "Official MinerU upload failed"
            ) from exc

    async def _download_markdown(self, url: str) -> str:
        body = bytearray()
        try:
            async with self._client.stream(
                "GET",
                url,
                follow_redirects=True,
                timeout=self.settings.parse_timeout_seconds,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.settings.result_max_bytes:
                        raise MinerUClientError(
                            "DOCUMENT_RESULT_TOO_LARGE",
                            "Official MinerU result is too large",
                        )
        except MinerUClientError:
            raise
        except httpx.TimeoutException as exc:
            raise MinerUClientError(
                "MINERU_TIMEOUT", "Official MinerU result timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise MinerUClientError(
                "MINERU_UNAVAILABLE", "Official MinerU result failed"
            ) from exc
        try:
            markdown = bytes(body).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MinerUClientError(
                "MINERU_PARSE_FAILED", "Official MinerU result is invalid"
            ) from exc
        if not markdown.strip():
            raise MinerUClientError(
                "MINERU_PARSE_FAILED", "Official MinerU result is empty"
            )
        return markdown

    async def _json_response(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 429:
            raise MinerUClientError(
                "MINERU_RATE_LIMITED", "Official MinerU rate limit exceeded"
            )
        if response.status_code >= 400:
            raise MinerUClientError(
                "MINERU_UNAVAILABLE", "Official MinerU returned an error"
            )
        body = response.content
        if len(body) > self.settings.result_max_bytes:
            raise MinerUClientError(
                "DOCUMENT_RESULT_TOO_LARGE", "Official MinerU response is too large"
            )
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MinerUClientError(
                "MINERU_PARSE_FAILED", "Official MinerU response is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise MinerUClientError(
                "MINERU_PARSE_FAILED", "Official MinerU response is invalid"
            )
        return payload

    @staticmethod
    def _asset_url(value: Any) -> str:
        url = str(value or "").strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise MinerUClientError(
                "MINERU_PARSE_FAILED",
                "Official MinerU asset URL is invalid",
            )
        return url


def _pdf_page_count(item: Any) -> int | None:
    if str(getattr(item, "extension", "")).lower() != ".pdf":
        return None
    try:
        from pypdf import PdfReader

        reader = PdfReader(Path(item.path), strict=False)
        if reader.is_encrypted:
            return None
        page_count = len(reader.pages)
    except Exception:
        return None
    return page_count if page_count > 0 else None


__all__ = ["OfficialFlashMinerUClient"]
