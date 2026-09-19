"""Machine-readable, redacted production delivery probe."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
from typing import Callable, Iterable

import httpx

from qwenpaw.config.config import AgentProfileConfig, Config

from .production_guard import (
    GuardResult,
    dependency_probe,
    validate_production_agent_profile,
    validate_production_root_config,
)


def _combine(*results: GuardResult) -> GuardResult:
    reasons = tuple(
        sorted({reason for result in results for reason in result.reason_codes})
    )
    return GuardResult(ready=not reasons, reason_codes=reasons)


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("top_level_object_required")
    return payload


def probe_delivery_files(
    *,
    root_config_path: Path,
    agent_config_paths: Iterable[Path],
    importer: Callable[[str], object] = importlib.import_module,
) -> GuardResult:
    """Validate native config schemas, policy and required imports."""
    results: list[GuardResult] = [dependency_probe(importer=importer)]
    try:
        root_payload = _load_json(root_config_path)
        Config.model_validate(root_payload)
        results.append(validate_production_root_config(root_payload))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        results.append(GuardResult(False, ("invalid_root_config",)))
    for path in agent_config_paths:
        try:
            agent_payload = _load_json(path)
            AgentProfileConfig.model_validate(agent_payload)
            results.append(validate_production_agent_profile(agent_payload))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            results.append(GuardResult(False, ("invalid_agent_config",)))
    return _combine(*results)


def probe_http_health(
    *,
    base_url: str,
    service_token: str,
    agent_id: str,
) -> GuardResult:
    if not base_url or not service_token or not agent_id:
        return GuardResult(False, ("health_probe_configuration_missing",))
    try:
        response = httpx.get(
            base_url.rstrip("/") + f"/api/bank-runtime/agents/{agent_id}/health",
            headers={
                "Authorization": f"Bearer {service_token}",
                "X-Agent-Id": agent_id,
            },
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        )
        if response.status_code != 200:
            return GuardResult(False, ("bank_runtime_health_failed",))
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return GuardResult(False, ("bank_runtime_health_failed",))
    except (httpx.HTTPError, ValueError, TypeError):
        return GuardResult(False, ("bank_runtime_health_failed",))
    return GuardResult(True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-config", type=Path, required=True)
    parser.add_argument(
        "--agent-config",
        type=Path,
        action="append",
        required=True,
    )
    parser.add_argument("--health-base-url", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = probe_delivery_files(
        root_config_path=args.root_config,
        agent_config_paths=args.agent_config,
    )
    if args.health_base_url:
        result = _combine(
            result,
            probe_http_health(
                base_url=args.health_base_url,
                service_token=os.environ.get("QWENPAW_SERVICE_TOKEN", ""),
                agent_id=os.environ.get(
                    "QWENPAW_RUNTIME_HEALTH_AGENT_ID",
                    "",
                ),
            ),
        )
    print(result.public_payload())
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "probe_delivery_files", "probe_http_health"]
