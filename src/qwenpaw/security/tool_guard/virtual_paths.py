"""Lexical path view for an already verified container tool installation.

This view changes Guard path interpretation, never file or tool authority.
Physical symlink and mount enforcement remains with the container executor.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
import posixpath
import re


@dataclass(frozen=True)
class ContainerGuardPaths:
    task_id: str
    root: str = '/workspace'
    cwd: str = '/workspace/scratch'
    home: str = '/workspace/scratch'
    literal_arguments: bool = False

    def __post_init__(self):
        if not isinstance(self.task_id,str) or not self.task_id:
            raise ValueError('Container Guard task identity is required')
        for value in (self.root,self.cwd,self.home):
            if (not isinstance(value,str) or not value.startswith('/') or '\\' in value
                    or '\x00' in value or '$' in value or '~' in value or posixpath.normpath(value)!=value):
                raise ValueError('Invalid container Guard path')
        for value in (self.cwd,self.home):
            PurePosixPath(value).relative_to(self.root)

    def expand(self,value):
        value=str(value)
        value=re.sub(r'\$(?:\{HOME\}|HOME\b)',lambda _:self.home,value)
        if value=='~' or value.startswith('~/'):value=self.home+value[1:]
        return value

    def resolve(self,value,*,base=None,expand=None):
        use_expansion = not self.literal_arguments if expand is None else expand
        expanded=self.expand(value) if use_expansion else str(value)
        if '\x00' in expanded:raise ValueError('Invalid container path')
        if not expanded.startswith('/'):
            expanded=posixpath.join(str(base) if base is not None else self.cwd,expanded)
        return PurePosixPath(posixpath.normpath(expanded))

    def argument_path(self, value):
        """Mirror the existing native file argument namespace, without I/O.

        Root input/public/scratch/output prefixes are workspace-relative;
        other relative file arguments are scratch-relative. They are literal
        filenames, while shell command tokens keep their shell path semantics.
        Validation and execution still belong to the original Gateway contract.
        """
        raw = str(value)
        first = raw.split('/', 1)[0]
        base = self.root if first in {'input', 'public', 'scratch', 'output'} else self.home
        return self.resolve(raw, base=base, expand=False)

    def for_file_arguments(self):
        return replace(self, literal_arguments=True)

    def for_cwd(self,value=None):
        if value is None:return self
        # A model cwd is a path, never permission to activate this view.
        from pathlib import PurePosixPath
        if not isinstance(value,str) or not value or '\\' in value or '\x00' in value:
            raise ValueError('Invalid container cwd')
        candidate=self.argument_path(value)
        candidate.relative_to(self.root)
        return replace(self,cwd=str(candidate))


_CURRENT=ContextVar('tool_guard_container_paths',default=None)


def current_container_paths():
    return _CURRENT.get()


@contextmanager
def container_path_view(paths):
    if not isinstance(paths,ContainerGuardPaths):
        raise TypeError('A verified installation must supply typed container paths')
    token=_CURRENT.set(paths)
    try:yield paths
    finally:_CURRENT.reset(token)
