"""Loopback-only fake MinerU service for local MCP integration verification."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
import uvicorn

_FILENAME = re.compile(rb'filename="([^"\\r\\n]+)"')


def create_app(*, expected_token: str | None = None) -> FastAPI:
    app = FastAPI(title="Fake MinerU", docs_url=None, redoc_url=None)
    token = expected_token or os.environ.get(
        "FAKE_MINERU_TOKEN",
        "local-mineru-token",
    )
    request_paths: list[str] = []
    task_results: dict[str, dict[str, Any]] = {}

    def authorize(request: Request) -> None:
        if request.headers.get("authorization") != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthorized")

    async def parse_result(request: Request) -> dict[str, Any]:
        authorize(request)
        body = await request.body()
        try:
            filenames = [
                value.decode("ascii", errors="strict")
                for value in _FILENAME.findall(body)
            ]
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=422,
                detail="filename is not opaque",
            ) from exc
        if not filenames:
            raise HTTPException(status_code=422, detail="files are required")
        if len(filenames) > 5:
            raise HTTPException(status_code=422, detail="too many files")
        results = {}
        for filename in filenames:
            stem = Path(filename).stem
            if not stem.startswith("file_"):
                raise HTTPException(status_code=422, detail="filename is not opaque")
            results[stem] = {
                "md_content": (
                    f"# Fake MinerU result for {stem}\n\n"
                    + "这是本机开发验证生成的脱敏解析正文。\n" * 1500
                ),
                "page_count": 12,
            }
        return {"version": "fake-http-1", "results": results}

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        authorize(request)
        request_paths.append(request.url.path)
        return {"status": "healthy", "version": "fake-http-1"}

    @app.post("/file_parse")
    async def file_parse(request: Request) -> dict[str, Any]:
        request_paths.append(request.url.path)
        return await parse_result(request)

    @app.post("/tasks")
    async def submit_task(request: Request) -> dict[str, str]:
        request_paths.append(request.url.path)
        task_id = f"fake_{uuid4().hex}"
        task_results[task_id] = await parse_result(request)
        return {"task_id": task_id}

    @app.get("/tasks/{task_id}")
    async def task_status(task_id: str, request: Request) -> dict[str, str]:
        authorize(request)
        request_paths.append(request.url.path)
        if task_id not in task_results:
            raise HTTPException(status_code=404, detail="task not found")
        return {"task_id": task_id, "status": "completed"}

    @app.get("/tasks/{task_id}/result")
    async def task_result(task_id: str, request: Request) -> dict[str, Any]:
        authorize(request)
        request_paths.append(request.url.path)
        if task_id not in task_results:
            raise HTTPException(status_code=404, detail="task not found")
        return task_results[task_id]

    @app.get("/_stats")
    async def stats(request: Request) -> dict[str, Any]:
        authorize(request)
        return {
            "request_count": len(request_paths),
            "request_paths": list(request_paths),
            "task_count": len(task_results),
        }

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("fake MinerU must listen on loopback")
    uvicorn.run(
        app, host=args.host, port=args.port, log_level="warning", access_log=False
    )


if __name__ == "__main__":
    main()
