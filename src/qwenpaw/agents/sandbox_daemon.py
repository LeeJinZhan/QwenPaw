# -*- coding: utf-8 -*-
"""Task-container daemon for stateful QwenPaw browser execution."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .tools.browser_control import browser_use


SOCKET_PATH = Path(os.environ.get("QWENPAW_SANDBOX_SOCKET", "/tmp/qwenpaw-sandbox.sock"))
MAX_REQUEST_BYTES = 2 * 1024 * 1024


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        raw = await reader.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request_too_large")
        request = json.loads(raw.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("operation_not_allowed")
        operation = str(request.get("operation", ""))
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("invalid_arguments")
        if operation == "browser.execute":
            # A headed browser would escape the task's isolated display boundary.
            arguments["headed"] = False
            # The container itself is task-ephemeral; a persistent CDP profile adds
            # startup latency and can outlive a disconnected caller without adding
            # useful persistence. Keep browser state inside the daemon process.
            if str(arguments.get("action", "")).strip().lower() == "start":
                arguments["private_mode"] = True
            response = await browser_use(**arguments)
            blocks = [dict(block) for block in response.content]
            text = "\n".join(
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("text") is not None
            )
            data: dict[str, Any] = {"content": blocks, "text": text}
        else:
            raise ValueError("operation_not_allowed")
        payload = {"ok": True, "data": data}
    except Exception as exc:
        payload = {
            "ok": False,
            "error_code": "sandbox_browser_failed",
            "error_type": type(exc).__name__,
        }
    writer.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    await writer.drain()
    writer.close()
    await writer.wait_closed()

async def serve() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(_handle, path=str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o600)
    async with server:
        await server.serve_forever()


def main() -> int:
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
