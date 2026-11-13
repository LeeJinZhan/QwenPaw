import json
import hashlib
import sys
from pathlib import Path
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from bank_runtime.session_export import export_session
from bank_runtime.remote_state import canonical_hash


def source(tmp_path, **changes):
    saved = {'bank_runtime_scope': {'agent_id':'agent-a','user_id':'user-a','channel':'bank-runtime',
        'session_id':'session-a','last_committed_task_id':'task-a'},
        'agent':{'state':{'session_id':'session-a','context':[{'id':'m','name':'assistant','role':'assistant',
            'content':[{'type':'text','text':'确认结果'}]}], 'summary':''},
            'scroll':{'seq_by_id':{'m':1},'persisted_ids':['m']}}}
    saved.update(changes)
    path = tmp_path / 'session.json'
    path.write_text(json.dumps(saved), encoding='utf-8')
    return path


def test_export_keeps_committed_context_without_local_index(tmp_path):
    path = source(tmp_path)
    raw = path.read_bytes()
    bundle = export_session(path)
    assert 'scroll' not in bundle['agent_state']
    assert bundle['agent_state']['state']['context'][0]['id'] == 'm'
    assert bundle.pop('bundle_hash') == canonical_hash(bundle)
    assert path.read_bytes() == raw


def test_export_rejects_uncommitted_session(tmp_path):
    with pytest.raises(ValueError):
        export_session(source(tmp_path, bank_runtime_scope={}))


def test_export_binds_results_and_rejects_path_escape(tmp_path):
    path = source(tmp_path)
    root = tmp_path / 'tool_results'
    root.mkdir()
    result = root / 'a.txt'
    result.write_text('工具完整结果', encoding='utf-8')
    bundle = export_session(path, result_root=root, result_files=[result])
    assert bundle['results'][0]['content'] == '工具完整结果'
    with pytest.raises(ValueError):
        export_session(path, result_root=root, result_files=[path])
    link = root / 'link.txt'
    link.symlink_to(result)
    with pytest.raises(ValueError):
        export_session(path, result_root=root, result_files=[link])


def test_export_rewrites_truncation_recall_without_local_file_dependency(tmp_path):
    from qwenpaw.agents.tools.utils import build_truncation_metadata
    path = source(tmp_path)
    root=tmp_path/'custom-cache'; root.mkdir()
    result=root/'result.txt'; result.write_text('全文',encoding='utf-8')
    saved=json.loads(path.read_text())
    metadata=build_truncation_metadata(file_path=str(result),file_size_bytes=6,total_lines=1,
        start_line=1,max_bytes=3,excerpt_bytes=3,read_from=1)
    saved['agent']['state']['context'][0]['metadata']=metadata
    path.write_text(json.dumps(saved),encoding='utf-8')
    with pytest.raises(ValueError):
        export_session(path)
    bundle=export_session(path,result_root=root,result_files=[result])
    info=bundle['agent_state']['state']['context'][0]['metadata']['qwenpaw_truncation']['0']
    assert 'read_runtime_result' in info['notice']
    assert str(result) not in info['notice']


def test_nested_results_are_migrated_in_dependency_order(tmp_path):
    path = source(tmp_path)
    root = tmp_path/'tool_results'; root.mkdir()
    leaf = root/'leaf.txt'; leaf.write_text('confirmed leaf',encoding='utf-8')
    parent = root/'parent.txt'; parent.write_text('Full result: '+str(leaf),encoding='utf-8')
    bundle = export_session(path,result_root=root,result_files=[parent,leaf])
    leaf_record = next(item for item in bundle['results'] if item['content']=='confirmed leaf')
    parent_record = next(item for item in bundle['results'] if item['content'].startswith('Full result:'))
    assert parent_record['content']=='Full result: '+leaf_record['result_ref']
    assert str(root) not in parent_record['content']
    assert len(bundle['results'])==2


def test_missing_nested_result_and_cycles_are_rejected_without_modifying_sources(tmp_path):
    path = source(tmp_path)
    root = tmp_path/'tool_results'; root.mkdir()
    a = root/'a.txt'; b = root/'b.txt'
    a.write_text(str(b),encoding='utf-8'); b.write_text(str(a),encoding='utf-8')
    with pytest.raises(ValueError):export_session(path,result_root=root,result_files=[a])
    with pytest.raises(ValueError):export_session(path,result_root=root,result_files=[a,b])
    assert a.read_text()==str(b)
    assert b.read_text()==str(a)


def test_explicit_archive_aliases_rewrite_nested_refs_and_preserve_user_prose(tmp_path):
    path=source(tmp_path)
    root=tmp_path/'restored';root.mkdir()
    leaf=root/'leaf.txt';leaf.write_text('full leaf',encoding='utf-8')
    old_leaf='/old/node/toolresult/leaf.txt'
    parent=root/'parent.txt';parent.write_text('See '+old_leaf,encoding='utf-8')
    old_parent=r'D:\old\toolresult\parent.txt'
    saved=json.loads(path.read_text())
    saved['agent']['state']['context'][0]['content'][0]['text']='Full: '+old_parent
    saved['agent']['state']['context'].append(dict(id='user',role='user',name='user',
        content=[dict(type='text',text='Example '+old_parent)]))
    path.write_text(json.dumps(saved),encoding='utf-8')
    aliases=[dict(source=old_leaf,file=str(leaf),sha256=hashlib.sha256(leaf.read_bytes()).hexdigest()),
             dict(source=old_parent,file=str(parent),sha256=hashlib.sha256(parent.read_bytes()).hexdigest())]
    bundle=export_session(path,result_root=root,result_files=[parent,leaf],result_aliases=aliases)
    messages=bundle['agent_state']['state']['context']
    assert 'runtime-result:' in messages[0]['content'][0]['text']
    assert messages[1]['content'][0]['text']=='Example '+old_parent
    assert any(item['content'].startswith('See runtime-result:') for item in bundle['results'])
    assert parent.read_text()=='See '+old_leaf


@pytest.mark.parametrize('failure',['hash','unselected','collision'])
def test_alias_mapping_cannot_add_unreviewed_or_changed_resources(tmp_path,failure):
    path=source(tmp_path)
    root=tmp_path/'results';root.mkdir()
    selected=root/'a.txt';selected.write_text('a')
    other=root/'b.txt';other.write_text('b')
    mapping=dict(source='/old/toolresult/a.txt',file=str(selected),sha256=hashlib.sha256(b'a').hexdigest())
    if failure=='hash':mapping['sha256']='0'*64
    if failure=='unselected':mapping.update(file=str(other),sha256=hashlib.sha256(b'b').hexdigest())
    if failure=='collision':mapping.update(source=str(other))
    with pytest.raises(ValueError):
        export_session(path,result_root=root,result_files=[selected,other] if failure=='collision' else [selected],
                       result_aliases=[mapping])


def test_alias_never_rewrites_a_longer_unselected_reference(tmp_path):
    path=source(tmp_path)
    root=tmp_path/'toolresult';root.mkdir()
    selected=root/'a.txt';selected.write_text('a')
    saved=json.loads(path.read_text())
    saved['agent']['state']['context'][0]['content'][0]['text']=str(selected)+'.missing'
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):export_session(path,result_root=root,result_files=[selected])
