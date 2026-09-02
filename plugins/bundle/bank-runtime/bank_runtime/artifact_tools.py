"""Runtime-executed office artifact tools.

These functions define the model-facing JSON schema only.  Bank Runtime's
Gateway middleware intercepts every admitted call and executes it in Runtime;
falling through to the local function is therefore a fail-closed condition.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any

from qwenpaw.exceptions import AgentRuntimeErrorException
from qwenpaw.hooks.base import LifecycleHook
from qwenpaw.runtime.hooks import HookContext, HookResult
from qwenpaw.runtime.phases import Phase


_UNMEDIATED = "成果工具必须通过 Bank Runtime Tool Gateway 执行。"
_SAFE_FORMAT = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_OPERATIONS = frozenset({"generate", "revise", "convert", "template_fill"})

ARTIFACT_RUNTIME_ACTION_BY_TOOL = {
    "artifact_generate": "artifact.generate",
    "artifact_revise": "artifact.revise",
    "artifact_convert": "artifact.convert",
    "template_fill_docx": "template.fill",
}
ARTIFACT_WORKER_TOOL_NAMES = frozenset(ARTIFACT_RUNTIME_ACTION_BY_TOOL)


@dataclass(frozen=True)
class ArtifactDeliveryIntent:
    """Trusted, structured marker for one required artifact delivery."""

    operation: str
    target_format: str
    source_refs: tuple[str, ...] = ()


class ArtifactToolNotInvokedError(AgentRuntimeErrorException):
    """The model answered in text instead of invoking an admitted tool."""

    def __init__(self) -> None:
        super().__init__(
            error_code="ARTIFACT_TOOL_NOT_INVOKED",
            message="明确的成果交付请求未调用受控成果工具",
            details={},
        )


class ArtifactDeliveryErrorHook(LifecycleHook):
    """Expose the stable artifact error without leaking candidate model text."""

    phase = Phase.ON_ERROR
    name = "bank_runtime_artifact_delivery_error"
    priority = 90

    async def run(self, ctx: HookContext) -> HookResult:
        if isinstance(ctx.error, ArtifactToolNotInvokedError):
            ctx.extras["_error_code"] = ctx.error.error_code
            ctx.extras["_error_text"] = ctx.error.message
        return HookResult()


def parse_artifact_delivery_intent(value: Any) -> ArtifactDeliveryIntent | None:
    """Parse only Runtime-authored structured intent; never infer from prose."""

    if not isinstance(value, Mapping):
        return None
    operation = str(value.get("operation") or "").strip().lower()
    target_format = str(value.get("target_format") or "").strip().lower()
    if (
        value.get("schema_version") != "1.0"
        or value.get("kind") != "artifact"
        or value.get("required") is not True
        or operation not in _OPERATIONS
        or not _SAFE_FORMAT.fullmatch(target_format)
    ):
        return None
    raw_refs = value.get("source_refs", [])
    if not isinstance(raw_refs, list):
        return None
    refs = tuple(str(item).strip() for item in raw_refs)
    if any(not item for item in refs) or len(set(refs)) != len(refs):
        return None
    return ArtifactDeliveryIntent(
        operation=operation,
        target_format=target_format,
        source_refs=refs,
    )


def artifact_delivery_intent_from_request(request: Any) -> ArtifactDeliveryIntent | None:
    """Read the same marker from the supported Runtime request envelopes."""

    direct = parse_artifact_delivery_intent(
        getattr(request, "output_requirement", None),
    )
    if direct is not None:
        return direct
    for field in ("request_context", "runtime_context"):
        container = getattr(request, field, None)
        if isinstance(container, Mapping):
            parsed = parse_artifact_delivery_intent(
                container.get("output_requirement"),
            )
            if parsed is not None:
                return parsed
    return None


async def artifact_generate(
    artifact_type: str,
    title: str,
    content: dict[str, Any] | list[Any] | str,
    instructions: str = "",
    source_refs: list[dict[str, str]] | None = None,
    output_name: str = "",
    explicit_pdf_request: bool = False,
) -> str:
    """Generate one controlled DOCX, XLSX, PPTX, or explicitly requested PDF.

    Args:
        artifact_type: One of ``docx``, ``xlsx``, ``pptx`` or ``pdf``.
        title: User-visible artifact title.
        content: Structured document, workbook, or presentation content.
        instructions: Optional bounded formatting or revision guidance.
        source_refs: Runtime-authorized source identifiers only.
        output_name: Optional safe output filename.
        explicit_pdf_request: Must be true only when the user asked for PDF.
    """
    del (
        artifact_type,
        title,
        content,
        instructions,
        source_refs,
        output_name,
        explicit_pdf_request,
    )
    return _UNMEDIATED


async def artifact_revise(
    source_generated_file_id: str,
    instructions: str,
    content: dict[str, Any] | list[Any] | str,
    output_name: str = "",
) -> str:
    """Create a new version of an existing Runtime-generated Office file.

    Args:
        source_generated_file_id: Runtime-generated source file identifier.
        instructions: Requested changes.
        content: Complete structured content for the new version.
        output_name: Optional safe output filename.
    """
    del source_generated_file_id, instructions, content, output_name
    return _UNMEDIATED


async def artifact_convert(
    source_generated_file_id: str,
    target_format: str,
    output_name: str = "",
    explicit_pdf_request: bool = False,
) -> str:
    """Convert a Runtime-generated artifact through an admitted worker.

    Args:
        source_generated_file_id: Runtime-generated source file identifier.
        target_format: Registered target format selected by Runtime.
        output_name: Optional safe output filename.
        explicit_pdf_request: Must be true only when the user asked for PDF.
    """
    del source_generated_file_id, target_format, output_name, explicit_pdf_request
    return _UNMEDIATED


async def template_fill_docx(
    template_version_id: str,
    title: str,
    fields: dict[str, Any],
    instructions: str = "",
    source_refs: list[dict[str, str]] | None = None,
    output_name: str = "",
) -> str:
    """Fill a published DOCX template already authorized by Runtime.

    Args:
        template_version_id: Published Runtime template version identifier.
        title: User-visible document title.
        fields: Template field values.
        instructions: Optional bounded content guidance.
        source_refs: Runtime-authorized source identifiers only.
        output_name: Optional safe output filename.
    """
    del template_version_id, title, fields, instructions, source_refs, output_name
    return _UNMEDIATED


__all__ = [
    "ARTIFACT_RUNTIME_ACTION_BY_TOOL",
    "ARTIFACT_WORKER_TOOL_NAMES",
    "ArtifactDeliveryErrorHook",
    "ArtifactDeliveryIntent",
    "ArtifactToolNotInvokedError",
    "artifact_convert",
    "artifact_delivery_intent_from_request",
    "artifact_generate",
    "artifact_revise",
    "parse_artifact_delivery_intent",
    "template_fill_docx",
]
