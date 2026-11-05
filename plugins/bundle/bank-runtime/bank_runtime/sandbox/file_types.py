"""Repair known attachment MIME labels only after identifying the actual format.

This does not grant access, decrypt, extract content, or replace upload admission.
"""
import codecs
from pathlib import Path
import zipfile

_DOCUMENT_MIMES = {
    '', 'application/octet-stream', 'application/pdf', 'application/msword', 'application/rtf',
    'application/vnd.ms-excel', 'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'text/plain', 'text/csv', 'text/tab-separated-values', 'text/markdown', 'application/csv',
}
_PACKAGES = {
    '.docx': ('word/document.xml', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    '.xlsx': ('xl/workbook.xml', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    '.pptx': ('ppt/presentation.xml', 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
}
_LEGACY = {'.doc': ('WordDocument', 'application/msword'),
           '.xls': ('Workbook', 'application/vnd.ms-excel'),
           '.ppt': ('PowerPoint Document', 'application/vnd.ms-powerpoint')}
_IMAGES = {'.png': ('PNG','image/png'), '.jpg': ('JPEG','image/jpeg'),
           '.jpeg': ('JPEG','image/jpeg'), '.webp': ('WEBP','image/webp')}
_IMAGE_MIMES = {'image/png', 'image/x-png', 'image/jpeg', 'image/jpg', 'image/pjpeg', 'image/webp', 'application/octet-stream'}


def canonical_attachment_mime(path: Path, declared: str) -> str:
    """Return the verified MIME, or the original normalized label on mismatch."""
    path = Path(path)
    mime = str(declared or '').split(';', 1)[0].strip().lower()
    suffix = path.suffix.lower()
    image = _IMAGES.get(suffix)
    if image and mime in _IMAGE_MIMES and mime != image[1]:
        from PIL import Image
        try:
            with Image.open(path) as value:
                if value.format == image[0]:
                    value.verify()
                    return image[1]
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
        return mime
    if mime not in _DOCUMENT_MIMES:
        return mime
    if suffix in {'.csv', '.tsv'}:
        expected = 'text/csv' if suffix == '.csv' else 'text/tab-separated-values'
        return expected if mime == expected or _valid_text(path) else mime
    with path.open('rb') as handle:
        prefix = handle.read(16)
    if suffix == '.pdf' and prefix.startswith(b'%PDF-'):
        return 'application/pdf'
    if suffix == '.doc' and prefix.startswith(br'{\rtf1'):
        return 'application/msword'
    package = _PACKAGES.get(suffix)
    if package and mime != package[1]:
        try:
            with zipfile.ZipFile(path) as archive:
                if {'[Content_Types].xml', package[0]} <= set(archive.namelist()):
                    return package[1]
        except (zipfile.BadZipFile, OSError):
            pass
    legacy = _LEGACY.get(suffix)
    if legacy and mime != legacy[1] and prefix.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        # CFB is the same container check used for correctly labelled legacy
        # Office attachments. Upload admission and the isolated format reader
        # retain macro/encryption and internal-stream validation respectively.
        return legacy[1]
    return mime


def _valid_text(path):
    with path.open('rb') as handle:
        prefix = handle.read(4)
    if prefix.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return False
    encodings = (('utf-8-sig',) if prefix.startswith(codecs.BOM_UTF8) else
                 ('utf-16',) if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else
                 ('utf-8', 'gb18030'))
    for encoding in encodings:
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
            present = False
            with path.open('rb') as handle:
                while value := handle.read(65536):
                    text = decoder.decode(value)
                    if any(ord(c) < 32 and c not in '\t\r\n' for c in text):
                        return False
                    present = present or bool(text)
                decoder.decode(b'', final=True)
            return present
        except UnicodeError:
            continue
    return False
