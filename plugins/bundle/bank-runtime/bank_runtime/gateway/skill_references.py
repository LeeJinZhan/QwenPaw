"""Bounded reference reads from an already visible native Skill.

References are instruction resources, never a general filesystem capability.
The Skill directory comes from the native registry, not a model-supplied path.
"""
import os
import re
import stat

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk
from agentscope.tool._builtin._skill import SkillViewer

MAX_REFERENCE_BYTES = 65536
_REFERENCE = re.compile(r'references/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.md\Z')
_LINK = re.compile(r'\[[^\]\n]*\]\((references/[^\s)]+)\)')


def read_reference(skill, reference: str) -> str:
    """Read one explicitly linked Markdown file, without following links."""
    if (not isinstance(reference, str) or not _REFERENCE.fullmatch(reference)
            or reference not in _LINK.findall(skill.markdown)):
        raise ValueError('Reference is not declared by this skill')
    directory = os.open(skill.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = reference.split('/')
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_REFERENCE_BYTES:
                raise ValueError('Reference is not a bounded regular file')
            data = source.read(MAX_REFERENCE_BYTES + 1)
        if not data or len(data) > MAX_REFERENCE_BYTES or b'\0' in data:
            raise ValueError('Reference is not bounded Markdown text')
        return data.decode('utf-8-sig')
    finally:
        os.close(directory)


class BankSkillViewer(SkillViewer):
    """Native Skill viewer with an optional, explicitly linked reference."""

    description = (SkillViewer.description + ' Read the skill main text first. '
                   'When a needed contract is linked under references/, retrieve only that '
                   'Markdown reference using the same skill name and the exact relative reference. '
                   'Do not read all references by default. Reading grants no execution permission.')
    input_schema = {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'skill': {'type': 'string', 'description': 'Exact visible skill name.'},
            'reference': {'type': 'string', 'description':
                'Optional references/*.md path explicitly linked in this skill main text. '
                'Omit to read the main text; not a host or sandbox path.'},
        },
        'required': ['skill'],
    }

    async def call(self, skill, _agent_state, reference=None):
        if reference is None:
            return await super().call(skill, _agent_state)
        skills = await self._get_skills_method(_agent_state.tool_context.activated_groups)
        target = skills.get(skill)
        try:
            if target is None:
                raise ValueError('Skill is not visible')
            text = read_reference(target, reference)
        except (OSError, ValueError):
            return ToolChunk(content=[TextBlock(text='Skill reference unavailable; do not use file or Shell tools to bypass this boundary.')],
                             state=ToolResultState.ERROR)
        return ToolChunk(content=[TextBlock(text=text)])
