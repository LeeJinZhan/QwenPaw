"""Explicit MinerU REST adapter with no protocol fallback."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import httpx

from .config import MinerUSettings

_REMOTE_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_LANGUAGE = {"auto": "ch", "zh": "ch", "en": "en"}


class MinerUClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MinerUHttpClient:
    def __init__(
        self,
        settings: MinerUSettings,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.parse_timeout_seconds,
                write=settings.upload_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
            follow_redirects=False,
        )

    async def probe(self) -> dict[str, Any]:
        try:
            response = await self._client.get(
                self._url("/health"),
                headers=self._headers(),
                timeout=self.settings.connect_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise MinerUClientError(
                "MINERU_UNAVAILABLE", "MinerU health probe failed"
            ) from exc
        payload = await self._json_response(response)
        if response.status_code != 200 or payload.get("status") != "healthy":
            raise MinerUClientError("MINERU_UNAVAILABLE", "MinerU is not healthy")
        return payload

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
        if self.settings.submit_mode == "file_parse":
            payload = await self._submit(
                "/file_parse",
                files,
                upload_stems,
                parse_method=parse_method,
                language=language,
                tables=tables,
                formulas=formulas,
                ambiguous_timeout=False,
            )
            return payload, upload_stems
        submission = await self._submit(
            "/tasks",
            files,
            upload_stems,
            parse_method=parse_method,
            language=language,
            tables=tables,
            formulas=formulas,
            ambiguous_timeout=True,
        )
        remote_task_id = str(submission.get("task_id") or "")
        if not _REMOTE_TASK_ID.fullmatch(remote_task_id):
            raise MinerUClientError(
                "MINERU_SUBMIT_AMBIGUOUS",
                "MinerU task submission response is invalid",
            )
        await self._wait_for_task(remote_task_id)
        result = await self._request_json(
            "GET",
            f"/tasks/{remote_task_id}/result",
        )
        return result, upload_stems

    async def close(self) -> None:
        await self._client.aclose()

    async def _submit(
        self,
        endpoint: str,
        files: Sequence[Any],
        upload_stems: Mapping[str, str],
        *,
        parse_method: str,
        language: str,
        tables: bool,
        formulas: bool,
        ambiguous_timeout: bool,
    ) -> dict[str, Any]:
        handles = []
        multipart = []
        try:
            for item in files:
                handle = Path(item.path).open("rb")
                handles.append(handle)
                filename = f"{upload_stems[item.file_id]}{item.extension}"
                multipart.append(("files", (filename, handle, item.media_type)))
            data = {
                "lang_list": _LANGUAGE.get(language, language),
                "parse_method": parse_method,
                "formula_enable": str(bool(formulas)).lower(),
                "table_enable": str(bool(tables)).lower(),
                "return_md": "true",
                "return_middle_json": "false",
                "return_model_output": "false",
                "return_content_list": "false",
                "return_images": "false",
                "response_format_zip": "false",
                "return_original_file": "false",
            }
            try:
                async with self._client.stream(
                    "POST",
                    self._url(endpoint),
                    headers=self._headers(),
                    data=data,
                    files=multipart,
                ) as response:
                    return await self._json_response(response)
            except httpx.ConnectError as exc:
                raise MinerUClientError(
                    "MINERU_UNAVAILABLE", "MinerU connection failed"
                ) from exc
            except httpx.TimeoutException as exc:
                code = (
                    "MINERU_SUBMIT_AMBIGUOUS" if ambiguous_timeout else "MINERU_TIMEOUT"
                )
                raise MinerUClientError(code, "MinerU request timed out") from exc
            except httpx.HTTPError as exc:
                raise MinerUClientError(
                    (
                        "MINERU_SUBMIT_AMBIGUOUS"
                        if ambiguous_timeout
                        else "MINERU_UNAVAILABLE"
                    ),
                    "MinerU request failed",
                ) from exc
        finally:
            for handle in handles:
                handle.close()

    async def _wait_for_task(self, task_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.parse_timeout_seconds
        while True:
            if loop.time() >= deadline:
                raise MinerUClientError("MINERU_TIMEOUT", "MinerU task timed out")
            status = await self._request_json("GET", f"/tasks/{task_id}")
            state = str(status.get("status") or "")
            if state == "completed":
                return
            if state == "failed":
                raise MinerUClientError("MINERU_PARSE_FAILED", "MinerU task failed")
            if state not in {"pending", "processing"}:
                raise MinerUClientError(
                    "MINERU_PARSE_FAILED", "MinerU task status is invalid"
                )
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _request_json(self, method: str, endpoint: str) -> dict[str, Any]:
        try:
            async with self._client.stream(
                method,
                self._url(endpoint),
                headers=self._headers(),
            ) as response:
                return await self._json_response(response)
        except httpx.TimeoutException as exc:
            raise MinerUClientError(
                "MINERU_TIMEOUT", "MinerU request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise MinerUClientError(
                "MINERU_UNAVAILABLE", "MinerU request failed"
            ) from exc

    async def _json_response(self, response: httpx.Response) -> dict[str, Any]:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self.settings.result_max_bytes:
                raise MinerUClientError(
                    "DOCUMENT_RESULT_TOO_LARGE", "MinerU response is too large"
                )
        if response.status_code >= 400:
            code = (
                "MINERU_PARSE_FAILED"
                if response.status_code in {400, 409, 422}
                else "MINERU_UNAVAILABLE"
            )
            raise MinerUClientError(code, "MinerU returned an error")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MinerUClientError(
                "MINERU_PARSE_FAILED", "MinerU response is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise MinerUClientError("MINERU_PARSE_FAILED", "MinerU response is invalid")
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.token}",
            "Accept": "application/json",
        }

    def _url(self, endpoint: str) -> str:
        return f"{self.settings.base_url}{endpoint}"


__all__ = ["MinerUClientError", "MinerUHttpClient"]
