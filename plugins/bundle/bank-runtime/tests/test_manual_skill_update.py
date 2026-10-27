"""The admin paste version must work with the unextended native viewer."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def test_manual_export_is_complete_and_reproducible(tmp_path):
    spec = importlib.util.spec_from_file_location('skill_export', ROOT / 'scripts/build_manual_skill_update.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.build(tmp_path / 'first')
    module.build(tmp_path / 'second')
    output = tmp_path / 'first'
    delivery = (output / 'bank-file-delivery.md').read_text()
    assert 'reference=' not in delivery and 'references/' not in delivery
    assert '按需读取格式契约' not in delivery
    assert 'artifact_status=succeeded' in delivery
    for reference in (ROOT / 'skills/bank-file-delivery/references').glob('*.md'):
        assert reference.read_text().strip() in delivery
    for name in module.SKILLS:
        content = (output / f'{name}.md').read_text()
        original = (ROOT / 'skills' / name / 'SKILL.md').read_text()
        assert re.search(r'^name: .+$', original, re.M).group() in content
        assert not re.search(r'reference=|\]\(references/', content)
        with zipfile.ZipFile(output / f'{name}.zip') as archive:
            assert archive.namelist() == [f'{name}/SKILL.md']
            assert archive.read(f'{name}/SKILL.md').decode() == content
        from qwenpaw.agents.skill_system.store import extract_zip_skills
        temporary, found = extract_zip_skills((output / f'{name}.zip').read_bytes())
        try:
            assert len(found) == 1
            assert found[0][1] == ('bank_assistant' if name == 'bank-assistant-zh' else name)
            assert (found[0][0] / 'SKILL.md').read_text() == content
        finally:
            shutil.rmtree(temporary)
    manifest = json.loads((output / 'manifest.json').read_text())
    for filename, digest in manifest['files'].items():
        assert hashlib.sha256((output / filename).read_bytes()).hexdigest() == digest
        assert (output / filename).read_bytes() == (tmp_path / 'second' / filename).read_bytes()
