"""Runtime-executed office artifact tools.

These functions define the model-facing JSON schema only.  Bank Runtime's
Gateway middleware intercepts every admitted call and executes it in Runtime;
falling through to the local function is therefore a fail-closed condition.
"""

from __future__ import annotations

from typing import Any


_UNMEDIATED = "成果工具必须通过 Bank Runtime Tool Gateway 执行。"


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


__all__ = ["artifact_generate", "artifact_revise", "template_fill_docx"]
