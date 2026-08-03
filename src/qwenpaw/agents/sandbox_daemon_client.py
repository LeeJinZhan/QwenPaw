# -*- coding: utf-8 -*-
"""Container-local CLI bridge used by Runtime's Docker executor."""
from __future__ import annotations

import json
import os
import socket
import sys
import time


SOCKET_PATH = os.environ.get("QWENPAW_SANDBOX_SOCKET", "/tmp/qwenpaw-sandbox.sock")


def _response_timeout_seconds() -> float:
    try:
        value = float(os.environ.get("QWENPAW_SANDBOX_CLIENT_TIMEOUT_SECONDS", "60"))
    except ValueError:
        value = 60.0
    return min(max(value, 1.0), 120.0)


def main() -> int:
    payload = sys.stdin.buffer.read(2 * 1024 * 1024 + 1)
    for attempt in range(20):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(_response_timeout_seconds())
                client.connect(SOCKET_PATH)
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                response = bytearray()
                while True:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    response.extend(chunk)
            parsed = json.loads(bytes(response).decode("utf-8"))
            sys.stdout.write(json.dumps(parsed, ensure_ascii=False, separators=(",", ":")))
            return 0 if parsed.get("ok") is True else 2
        except (FileNotFoundError, ConnectionRefusedError):
            if attempt == 19:
                break
            time.sleep(0.1)
        except Exception:
            break
    sys.stdout.write('{"ok":false,"error_code":"sandbox_browser_unavailable"}')
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
