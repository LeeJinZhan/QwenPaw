import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.events import CompactEventProjector


def test_phase_survives_tools_and_is_not_final_or_reasoning():
    projector = CompactEventProjector('task')
    chunks = projector.project({'object': 'message', 'type': 'message', 'id': 'm1',
                                'content': '我会先整理资料，再生成文件。', 'status': 'completed'})
    assert chunks[0]['text'].endswith('。')
    assert chunks[0]['message_id'] == 'm1'
    phase = projector.project({'object': 'message', 'type': 'plugin_call', 'id': 'call1'})
    assert phase == [{'event': 'answer.phase', 'message_id': 'm1', 'text': '我会先整理资料，再生成文件。'}]
    assert projector.project({'object': 'message', 'type': 'plugin_call_output', 'id': 'call1'}) == []
    projector.project({'object': 'message', 'type': 'reasoning', 'id': 'r1', 'content': '思考内容'})
    final = projector.project({'object': 'message', 'type': 'message', 'id': 'm2', 'content': '文件已生成。'})
    assert final == [{'event': 'answer.chunk', 'message_id': 'm2', 'text': '文件已生成。'}]
    assert [e['event'] for e in projector.project({'object': 'response', 'status': 'completed'})] == ['answer.completed']


def test_late_reasoning_classification_never_becomes_phase():
    projector = CompactEventProjector('task')
    projector.project({'object': 'message', 'type': 'message', 'id': 'm1', 'content': '思考'})
    events = projector.project({'object': 'message', 'type': 'reasoning', 'id': 'm1', 'content': '思考'})
    assert all(e['event'] != 'answer.phase' for e in events)
    assert projector.project({'object': 'message', 'type': 'plugin_call', 'id': 'call1'}) == []
