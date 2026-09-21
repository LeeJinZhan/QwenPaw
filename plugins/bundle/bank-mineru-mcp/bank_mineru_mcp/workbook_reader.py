"""Read-only OOXML adapter with disk-backed shared strings.

Uses the pinned openpyxl reader extension point, isolated here and covered by
OOXML shared-string tests. Neither formulas nor external links are evaluated.
"""
from contextlib import contextmanager
from functools import lru_cache
import sqlite3
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from openpyxl.reader.excel import ExcelReader
from openpyxl.xml.constants import SHARED_STRINGS
from openpyxl.cell.text import Text


class DiskStrings:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.execute('PRAGMA cache_size=-2048')
        self.connection.execute('PRAGMA temp_store=FILE')
        self.connection.execute('CREATE TABLE strings (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
        self.get = lru_cache(maxsize=128)(self._get)

    def _get(self, index):
        row = self.connection.execute('SELECT value FROM strings WHERE id=?', (index,)).fetchone()
        if row is None:
            raise IndexError('Shared string index is absent')
        return row[0]

    def __getitem__(self, index):
        return self.get(index)

    def load(self, source):
        stack = []
        index = 0
        for event, element in ElementTree.iterparse(source, events=('start', 'end')):
            if event == 'start':
                stack.append(element)
                continue
            if element.tag.endswith('}si'):
                value = Text.from_tree(element).content.replace('x005F_', '')
                self.connection.execute('INSERT INTO strings VALUES (?,?)', (index, value))
                index += 1
                if len(stack) > 1:
                    stack[-2].remove(element)
                element.clear()
            stack.pop()
        self.connection.commit()

    def close(self):
        self.get.cache_clear()
        self.connection.close()


@contextmanager
def open_workbooks(path, temporary_root):
    readers = []
    with tempfile.TemporaryDirectory(prefix='.strings-', dir=temporary_root) as directory:
        strings = DiskStrings(Path(directory) / 'strings.sqlite')
        loaded = False
        class Reader(ExcelReader):
            def read_strings(self):
                nonlocal loaded
                content = self.package.find(SHARED_STRINGS)
                if content is not None:
                    if not loaded:
                        with self.archive.open(content.PartName.lstrip('/')) as source:
                            strings.load(source)
                        loaded = True
                    self.shared_strings = strings
        try:
            for data_only in (True, False):
                reader = Reader(path, read_only=True, data_only=data_only, keep_links=False)
                readers.append(reader)
                reader.read()
            yield tuple(reader.wb for reader in readers)
        finally:
            for reader in readers:
                reader.archive.close()
            strings.close()
