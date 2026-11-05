"""Bounded passive OOXML reading copy; originals and external resources stay untouched."""
from contextlib import contextmanager
from pathlib import PurePosixPath, Path
from tempfile import TemporaryDirectory
from lxml import etree as ET
import zipfile

_REL_NAMESPACES = {
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
    'http://purl.oclc.org/ooxml/officeDocument/relationships',
}


def _xml(body):
    # Reading documents never requires a DTD or entities.
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('Office XML declarations are unsupported')
    return ET.fromstring(body, ET.XMLParser(resolve_entities=False, load_dtd=False, no_network=True))


@contextmanager
def passive_office_copy(path, *, max_bytes=200 * 1024**2):
    if not zipfile.is_zipfile(path):
        yield path, 0
        return
    with zipfile.ZipFile(path) as source:
        infos = source.infolist()
        if len(infos) > 20000 or sum(info.file_size for info in infos) > max_bytes:
            raise ValueError('Office expansion quota exceeded')
        replacements, owners = {}, {}
        removed = 0
        for info in infos:
            if not info.filename.endswith('.rels'):
                continue
            root = _xml(source.read(info))
            rejected = set()
            for rel in list(root):
                target = rel.get('Target', '').strip().lower()
                external = rel.get('TargetMode', '').strip().lower() == 'external' or target.startswith(('http:', 'https:', 'file:', 'ftp:', '//'))
                if not external:
                    continue
                if rel.get('Type', '').endswith('/hyperlink') and target.startswith(('http://', 'https://')):
                    continue
                rejected.add(rel.get('Id', ''))
                root.remove(rel)
                removed += 1
            if rejected:
                replacements[info.filename] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
                relpath = PurePosixPath(info.filename)
                owner = str(relpath.parent.parent / relpath.name.removesuffix('.rels'))
                if owner in source.namelist():
                    owners.setdefault(owner, set()).update(rejected)
        if not removed:
            yield path, 0
            return
        for owner, rejected in owners.items():
            root = _xml(source.read(owner))
            for parent in root.iter():
                for child in list(parent):
                    if any(key.startswith('{' + namespace + '}') and value in rejected
                           for key, value in child.attrib.items() for namespace in _REL_NAMESPACES):
                        parent.remove(child)
            replacements[owner] = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        with TemporaryDirectory(prefix='office-passive-') as directory:
            destination = Path(directory) / path.name
            with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                for info in infos:
                    # No filesystem extraction, URL access, macro execution or source mutation.
                    output.writestr(info.filename, replacements[info.filename] if info.filename in replacements else source.read(info))
            yield destination, removed
