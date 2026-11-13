"""Request-owned, disposable copy of the configuration actually verified.

These files are a cache, never a publication authority. They live until FINALLY,
so lazy skill loaders cannot reread a different version halfway through a turn.
No snapshot is shared between requests or serialized with provider credentials.
"""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import unquote, urlsplit

from .remote_state import canonical_hash


class ExecutionConfiguration:
    MAX_FILES = 2048
    MAX_BYTES = 64 * 1024 * 1024

    def __init__(self, config, provider, skills, modes):
        self.config = copy.deepcopy(config)
        self.provider = copy.deepcopy(provider)
        self.skills, self.modes = list(skills), frozenset(modes)
        self.base_prompt = None
        self._temporary = tempfile.TemporaryDirectory(prefix='bank-config-')
        self._directories = None
        self._versions = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._temporary.cleanup()

    @property
    def skills_prepared(self):
        return self._directories is not None

    def skill_directories(self, sources=None):
        if self._directories is not None:
            return list(self._directories)
        if sources is None:
            raise ValueError('Execution skill snapshot is not prepared')
        directories, versions, total, count = [], [], 0, 0
        for index, source in enumerate(dict.fromkeys(sources)):
            root = Path(source)
            if root.is_symlink() or not root.is_dir():
                raise ValueError('Invalid skill resource directory')
            target = Path(self._temporary.name) / str(index) / root.name
            records = []
            for parent, children, files in os.walk(root, followlinks=False):
                children.sort()
                if any((Path(parent) / child).is_symlink() for child in children):
                    raise ValueError('Invalid symlinked skill resource')
                for name in sorted(files):
                    path = Path(parent) / name
                    mode = path.lstat().st_mode
                    if not stat.S_ISREG(mode):
                        raise ValueError('Invalid skill resource')
                    count += 1
                    if count > self.MAX_FILES:
                        raise ValueError('Skill snapshot file limit')
                    with path.open('rb') as stream:
                        content = stream.read(self.MAX_BYTES - total + 1)
                    total += len(content)
                    if total > self.MAX_BYTES:
                        raise ValueError('Skill snapshot byte limit')
                    relative = path.relative_to(root)
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                    # Preserve executable scripts, without setuid or writable
                    # permissions. The owning process can dispose of the cache.
                    destination.chmod(0o500 if mode & 0o111 else 0o400)
                    records.append({'path': relative.as_posix(), 'hash': hashlib.sha256(content).hexdigest()})
            if not (target / 'SKILL.md').is_file():
                raise ValueError('Skill snapshot requires SKILL.md')
            self._check_references(target)
            directories.append(str(target))
            versions.append({'id': root.name, 'hash': canonical_hash(records)})
        self._directories, self._versions = directories, versions
        return list(directories)

    @staticmethod
    def _check_references(root):
        for document in root.rglob('*.md'):
            text = document.read_text(encoding='utf-8')
            # Markdown local links (including nested reference documents).
            # External links and anchors are not resources in this bundle.
            for value in re.findall(r'\]\(\s*(<[^>]+>|[^\s)]+)', text):
                link = value.strip('<>')
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                target = (document.parent / unquote(parsed.path)).resolve()
                if not target.is_relative_to(root.resolve()) or not target.exists():
                    raise ValueError('Missing or escaping skill reference')

    def skill_versions(self):
        return copy.deepcopy(self._versions)

    def normalize_paths(self, text):
        for index, directory in enumerate(self._directories or []):
            text = text.replace(directory, '<skill:' + str(index) + '>')
        return text
