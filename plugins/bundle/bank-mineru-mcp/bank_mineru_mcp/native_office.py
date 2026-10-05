"""Independent, pinned DocVortex adapter; no OCR or implicit conversion."""
from __future__ import annotations

from importlib.metadata import version
from importlib import import_module
from contextlib import contextmanager
import json
import hashlib
import io
from pathlib import Path
import re
import warnings
from threading import RLock
import zipfile
from xml.etree import ElementTree as ET

from .spreadsheet import SpreadsheetExtractError

ENGINE = "docvortex-0.5.4"
SUPPORTED = frozenset({".doc", ".docx", ".ppt", ".pptx"})
_NATIVE_PARSE_LOCK = RLock()


@contextmanager
def _guard_native_pixels(rejected):
    """Scope a pinned-engine serializer guard to this isolated parsing call.

    DocVortex 0.5.4 opens pictures before exposing assets. Its serializers
    already accept None to skip an image; catch unsafe dimensions at that
    boundary without changing input files or weakening Pillow protection.
    The lock also serializes the explicitly selected development adapter.
    """
    from PIL import Image
    with _NATIVE_PARSE_LOCK:
        originals = []
        try:
            for name in ('doc.doc_converter', 'docx.resources', 'docx.tables',
                         'ppt.parser', 'pptx.resources', 'rtf.converter'):
                module = import_module('docvortex.analyzers.native.office.' + name)
                original = module.serialize_office_image
                def guarded(payload, *args, _serialize=original, **kwargs):
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter('error', Image.DecompressionBombWarning)
                            with Image.open(io.BytesIO(payload)) as image:
                                if image.width * image.height > 50_000_000:
                                    rejected.add(hashlib.sha256(payload).hexdigest())
                                    return None
                    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
                        rejected.add(hashlib.sha256(payload).hexdigest())
                        return None
                    except (OSError, ValueError):
                        # Non-raster/vector formats remain the engine's job.
                        pass
                    return _serialize(payload, *args, **kwargs)
                originals.append((module, original))
                module.serialize_office_image = guarded
            yield
        finally:
            for module, original in reversed(originals):
                module.serialize_office_image = original


def extract_native_office(path: Path, target: Path, *, max_bytes: int, max_images: int = 200) -> dict:
    if type(max_images) is not int or not 1 <= max_images <= 10000:
        raise SpreadsheetExtractError('DOCUMENT_ARGUMENT_INVALID', 'Invalid native image limit')
    if path.suffix.lower() not in SUPPORTED:
        raise SpreadsheetExtractError("FILE_TYPE_UNSUPPORTED", "Unsupported native Office format")
    if version("docvortex") != "0.5.4":
        raise SpreadsheetExtractError("DOCUMENT_ENGINE_UNAVAILABLE", "Pinned native engine is required")
    import docvortex
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    inventory, notes = _source_inventory(path, max_bytes=max_bytes)
    rejected_pixels = set()
    with _guard_native_pixels(rejected_pixels):
        parsed = docvortex.parse(path, file_suffix=path.suffix.lower()[1:])
    artifact = docvortex.render_artifact(parsed.middle_json, "markdown", assets=parsed.assets)
    assets = []
    asset_bytes = 0
    image_diagnostics = ['native_image_pixel_limit'] if rejected_pixels else []
    image_export = {'max_images':max_images, 'candidates_total':len(parsed.assets) + len(rejected_pixels), 'exported':0,
        'skipped_count_limit':max(0,len(parsed.assets)-max_images), 'skipped_byte_limit':0,
        'skipped_pixel_limit':len(rejected_pixels), 'skipped_unreadable':0}
    from PIL import Image
    for index, (name, payload) in enumerate(parsed.assets.items(), 1):
        if index > max_images:
            image_diagnostics.append('native_image_count_limit')
            break
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    if image.width * image.height > 50_000_000:
                        image_diagnostics.append("native_image_pixel_limit")
                        image_export['skipped_pixel_limit'] += 1
                        continue
                    encoded_image = io.BytesIO()
                    image.convert("RGB").save(encoded_image, format="PNG")
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            image_diagnostics.append('native_image_pixel_limit')
            image_export['skipped_pixel_limit'] += 1
            continue
        except (OSError, ValueError):
            image_diagnostics.append("native_image_unreadable")
            image_export['skipped_unreadable'] += 1
            continue
        content = encoded_image.getvalue()
        if asset_bytes + len(content) > max_bytes:
            image_diagnostics.append('native_image_bytes_limit')
            image_export['skipped_byte_limit'] += 1
            continue
        asset_bytes += len(content)
        leaf = f"image_{index:03d}.png"
        (target / leaf).write_bytes(content)
        assets.append({"name": leaf, "sha256": hashlib.sha256(content).hexdigest()})
    markdown = artifact.content.decode("utf-8")
    # Notes are a separately labelled source, never spliced into slide body.
    if notes:
        markdown += "\n\n" + "\n\n".join(f"### 演讲备注（{name}）\n{text}" for name, text in notes)
    diagnostics = sorted({diagnostic.code for diagnostic in parsed.diagnostics + artifact.diagnostics} | set(image_diagnostics))
    image_export['exported'] = len(assets)
    result = {"engine": ENGINE, "markdown": markdown, "page_count": len(parsed.middle_json.pages),
              "source_inventory": inventory, "diagnostics": diagnostics, "image_assets": assets, 'image_export':image_export,
              "coverage": {"native_text": "parsed", "image_text": "unread" if inventory["images"] or assets else "not_present" if inventory["source_inventory_complete"] else "unknown",
                           "embedded_objects": "unread" if inventory["embedded_objects"] else "not_present" if inventory["source_inventory_complete"] else "unknown",
                           "formula_evaluation": "not_performed", "rendered_layout": "not_verified"}}
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    # Reserve the exact serialized text/metadata budget before publishing,
    # discarding image exports with a coverage gap rather than losing text.
    while assets and len(encoded) + asset_bytes > max_bytes:
        removed = assets.pop()
        removed_path = target / removed['name']
        asset_bytes -= removed_path.stat().st_size
        removed_path.unlink()
        image_export['exported'] = len(assets)
        image_export['skipped_byte_limit'] += 1
        result['diagnostics'] = sorted(set(result['diagnostics']) | {'native_image_bytes_limit'})
        encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
    if len(encoded) + asset_bytes > max_bytes:
        raise SpreadsheetExtractError("DOCUMENT_RESULT_TOO_LARGE", "Native result quota exceeded")
    (target / "native.json").write_bytes(encoded)
    return result


def _source_inventory(path, *, max_bytes):
    inventory = {"format": path.suffix.lower(), "tables": 0, "slides": 0, "notes": 0,
                 "images": 0, "embedded_objects": 0, "source_inventory_complete": False}
    notes = []
    if path.suffix.lower() in {".doc", ".ppt"}:
        # Binary Office object coverage is not inferred from an OOXML inventory.
        inventory["image_coverage"] = "unknown"
        return inventory, notes
    namespaces = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main",
                  "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos) > 512 * 1024**2 or len(infos) > 100_000:
            raise SpreadsheetExtractError("DOCUMENT_RESULT_TOO_LARGE", "Office expansion quota exceeded")
        for info in infos:
            name = info.filename
            if name.startswith(("word/media/", "ppt/media/")) and not info.is_dir():
                inventory["images"] += 1
            if name.startswith(("word/embeddings/", "ppt/embeddings/")) and not info.is_dir():
                inventory["embedded_objects"] += 1
            if name.startswith(("word/", "ppt/")) and name.endswith(".xml") and not name.endswith(".rels"):
                if info.file_size > min(max_bytes * 16, 128 * 1024**2):
                    raise SpreadsheetExtractError("DOCUMENT_RESULT_TOO_LARGE", "Office object quota exceeded")
                with archive.open(info) as stream:
                    # No expansion of external entities, and clear nodes to bound memory.
                    text_parts = []
                    for _, element in ET.iterparse(stream, events=("end",)):
                        tag = element.tag.rsplit("}", 1)[-1]
                        if tag == "tbl":
                            inventory["tables"] += 1
                        if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name) and tag == "t" and element.text:
                            text_parts.append(element.text)
                        element.clear()
                    if text_parts:
                        notes.append((name.rsplit("/", 1)[-1], "\n".join(text_parts)))
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", name):
                inventory["slides"] += 1
        inventory["notes"] = len(notes)
        inventory["source_inventory_complete"] = True
    return inventory, notes
