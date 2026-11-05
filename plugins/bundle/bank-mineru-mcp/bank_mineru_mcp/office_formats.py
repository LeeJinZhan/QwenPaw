"""Choose an Office parser from bytes inside the admitted reading job.

Recognition is not validation or conversion. Readers retain their structural,
encryption and resource checks; no source bytes or authorization metadata change.
"""
import zipfile

_PACKAGES = {'word/document.xml': '.docx', 'xl/workbook.xml': '.xlsx',
             'ppt/presentation.xml': '.pptx'}
_LEGACY = {'.doc': '.doc', '.docx': '.doc', '.xls': '.xls', '.xlsx': '.xls',
           '.ppt': '.ppt', '.pptx': '.ppt'}


def office_parser_format(path):
    """Return recognizable format, otherwise let the selected reader diagnose it.

    CFB is a container, not sufficient evidence of a particular Office format;
    the family-specific reader must still validate its internal streams.
    """
    suffix = path.suffix.lower()
    with path.open('rb') as stream:
        prefix = stream.read(16)
    if prefix.startswith(br'{\rtf1'):
        return '.rtf'
    if prefix.startswith(bytes.fromhex('d0cf11e0a1b11ae1')):
        return _LEGACY.get(suffix, suffix)
    if prefix.startswith(b'PK'):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                detected = [kind for name, kind in _PACKAGES.items() if name in names]
                if '[Content_Types].xml' in names and len(detected) == 1:
                    return detected[0]
        except zipfile.BadZipFile:
            pass
    return suffix
