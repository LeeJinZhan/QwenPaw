from copy import deepcopy
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentscope.tool import FunctionTool
from bank_runtime.artifact_tools import artifact_generate, ArtifactDeliveryIntent
from bank_runtime.artifact_schema import describe_docx_tools


@pytest.mark.asyncio
async def test_unfrozen_revision_schema_only_offers_generated_full_rebuild():
    import jsonschema
    from bank_runtime.artifact_tools import artifact_revise
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
    tool = FunctionTool(artifact_revise)
    tools = [{'type': 'function', 'function': {'name': tool.name, 'parameters': tool.input_schema}}]
    original = deepcopy(tools)
    async def model(**kwargs):
        schema = kwargs['tools'][0]['function']['parameters']
        for fmt, body in [('docx', {'paragraphs': ['完整正文']}),
                          ('xlsx', {'sheets': [{'name': '数据', 'rows': [[1]]}]}),
                          ('pptx', {'slides': [{'title': '标题', 'bullets': ['正文']}]})]:
            payload = {'source_generated_file_id': 'generated-example', 'instructions': '接受重新生成',
                       'revision_mode': 'rebuild', 'content': body}
            jsonschema.validate(payload, schema)
            for variant in ({**payload, 'revision_mode': 'patch'},
                            {k: v for k, v in payload.items() if k != 'revision_mode'},
                            {**payload, 'source_type': 'session_file', 'source_id': 'upload-example'}):
                with pytest.raises(jsonschema.ValidationError):
                    jsonschema.validate(variant, schema)
        return None
    await BankRuntimeGatewayMiddleware(None).on_model_call(None, {'tools': tools}, model)
    assert tools == original


@pytest.mark.parametrize('layout', ['', 'standard_document', 'official_document'])
@pytest.mark.parametrize('operation', ['generate', 'revise'])
@pytest.mark.parametrize('recovering', [False, True])
@pytest.mark.asyncio
async def test_model_call_rejects_paused_local_word_revision(layout, operation, recovering, monkeypatch):
    import jsonschema
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from agentscope.credential._openai import OpenAICredential
    from agentscope.message import ToolCallBlock
    from agentscope.model import ChatResponse
    from openai.types.chat import ChatCompletion
    from qwenpaw.providers.openai_chat_model_compat import OpenAIChatModelCompat
    from bank_runtime.artifact_tools import artifact_revise
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
    from bank_runtime.text_patch_contract import validate_text_patch

    tools = []
    for function in (artifact_generate, artifact_revise):
        tool = FunctionTool(function)
        tools.append({'type': 'function', 'function': {
            'name': tool.name, 'parameters': tool.input_schema}})
    original = deepcopy(tools)
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=ArtifactDeliveryIntent(
        operation, 'docx', layout_kind=layout, layout_resolution='skill'))
    if recovering:
        middleware._record_artifact_input_failure('artifact_revise', {
            'source_type': 'session_file', 'source_id': 'file-example',
            'revision_mode': 'patch', 'content': {'kind': 'docx_patch', 'edits': []}})

    prepared = {}
    async def model(**kwargs):
        prepared.update(kwargs)
        return ChatResponse(content=[ToolCallBlock(
            id='revision-probe', name='artifact_revise', input='{}')], is_last=True)

    await middleware.on_model_call(None, {'tools': tools}, model)
    schemas = {tool['function']['name']: tool['function']['parameters']
               for tool in prepared['tools']}
    patch = {'kind': 'docx_patch', 'edits': [{
        'target': {}, 'old_text': '原金额100万元', 'new_text': '原金额200万元'}]}
    validate_text_patch(patch, 'docx')
    payload = {'source_type': 'session_file', 'source_id': 'file-example',
               'instructions': '只改金额', 'revision_mode': 'patch', 'content': patch}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schemas['artifact_revise'])
    for malformed in ({'kind': 'docx_patch'}, {'kind': 'docx_patch', 'edits': []}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**payload, 'content': malformed}, schemas['artifact_revise'])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'artifact_type': 'docx', 'title': '修订', 'content': patch,
                            'delivery_plan': {'document_type': 'article', 'target_format': 'docx',
                                              'layout_kind': layout or 'standard_document'}},
                           schemas['artifact_generate'])
    # Check what the model actually receives, including the exception to layout guidance.
    seen = []
    async def sdk_call(*, messages, tools, **kwargs):
        prompt = str(messages)
        assert '修改位置和替换内容' in prompt
        assert messages[0]['role'] == 'system'
        assert sum(message['role'] == 'system' for message in messages) == 1
        schema = next(tool['function']['parameters'] for tool in tools
                      if tool['function']['name'] == 'artifact_revise')
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(payload, schema)
        seen.append(messages)
        return ChatCompletion(id='revision-probe', created=0, model='test', object='chat.completion',
            choices=[{'index': 0, 'finish_reason': 'stop',
                      'message': {'role': 'assistant', 'content': '测试回答'}}])
    monkeypatch.setattr('openai.AsyncClient', lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=sdk_call)))))
    for model_id in ('deepseek-v4-flash', 'qwen3-27b'):
        provider = OpenAIChatModelCompat(credential=OpenAICredential(
            id='revision-probe', api_key='unused', base_url='http://127.0.0.1:1/v1'),
            model=model_id, stream=False)
        await provider._call_api(model_id, prepared['messages'], prepared['tools'])
    assert len(seen) == 2
    assert tools == original


@pytest.mark.asyncio
async def test_rejected_patch_layout_plan_cannot_restore_paused_revision():
    import jsonschema
    from agentscope.message import ToolCallBlock
    from agentscope.model import ChatResponse
    from bank_runtime.artifact_tools import artifact_revise
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware

    payload = {'source_type': 'session_file', 'source_id': 'file-example',
               'instructions': '只改金额', 'revision_mode': 'patch',
               'content': {'kind': 'docx_patch', 'edits': [{
                   'target': {}, 'old_text': '100万元', 'new_text': '200万元'}]}}
    middleware = BankRuntimeGatewayMiddleware(None)
    middleware._record_artifact_input_failure('artifact_revise', {**payload, 'delivery_plan': {
        'document_type': 'notice', 'target_format': 'docx', 'layout_kind': 'official_document'}})
    tool = FunctionTool(artifact_revise)
    tools = [{'type': 'function', 'function': {'name': tool.name, 'parameters': tool.input_schema}}]
    calls = []
    async def model(**kwargs):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(payload, kwargs['tools'][0]['function']['parameters'])
        calls.append(kwargs)
        return ChatResponse(content=[ToolCallBlock(
            id='corrected-patch', name='artifact_revise', input='{}')], is_last=True)

    await middleware.on_model_call(None, {'tools': tools}, model)
    assert len(calls) == 1
    assert middleware.artifact_intent is None


def test_docx_schema_describes_nested_body_without_mutating_shared_tools():
    tool = FunctionTool(artifact_generate)
    tools = [{'type': 'function', 'function': {'name': tool.name, 'parameters': tool.input_schema}}]
    original = deepcopy(tools)
    result = describe_docx_tools(tools, ArtifactDeliveryIntent('generate', 'docx', layout_kind='standard_document'))
    content = result[0]['function']['parameters']['properties']['content']
    body = content
    assert body['type'] == 'object'
    assert body['properties']['sections']['items']['properties']['paragraphs']['items']['type'] == 'string'
    assert tools == original
    assert result[0]['function']['parameters']['properties']['artifact_type']['enum'] == ['docx']
    assert body['properties']['tables']['items']['items']['items']['type'] == 'string'
    assert describe_docx_tools(tools, None) is tools
    assert describe_docx_tools(tools, ArtifactDeliveryIntent('generate', 'pptx')) is tools
    official = describe_docx_tools(tools, ArtifactDeliveryIntent('generate', 'docx', layout_kind='official_document'))
    assert 'document' in official[0]['function']['parameters']['properties']['content']['properties']


def test_docx_model_schema_rejects_delivery_plan_nested_in_content():
    import jsonschema
    import pytest
    tool = FunctionTool(artifact_generate)
    tools = [{'type': 'function', 'function': {'name': tool.name, 'parameters': tool.input_schema}}]
    schema = describe_docx_tools(tools, ArtifactDeliveryIntent('generate', 'docx', layout_kind='standard_document', layout_resolution='skill'))[0]['function']['parameters']
    plan = {'document_type': 'article', 'target_format': 'docx', 'layout_kind': 'standard_document'}
    payload = {'artifact_type': 'docx', 'title': '说明', 'content': {'paragraphs': ['正文'], 'delivery_plan': plan}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
    payload['delivery_plan'] = payload['content'].pop('delivery_plan')
    jsonschema.validate(payload, schema)


def test_unresolved_docx_intent_has_explicit_layout_alternatives():
    tool = FunctionTool(artifact_generate)
    tools = [{'type': 'function', 'function': {'name': tool.name, 'parameters': tool.input_schema}}]
    schema = describe_docx_tools(tools, ArtifactDeliveryIntent('generate', 'docx', layout_resolution='skill'))[0]['function']['parameters']
    branches = schema['properties']['content']['anyOf']
    assert all(branch.get('additionalProperties') is False for branch in branches)
    assert any('document' in branch['properties'] for branch in branches)
    assert any('sections' in branch['properties'] for branch in branches)
    assert schema['properties']['delivery_plan']['properties']['layout_kind']['enum'] == ['official_document', 'standard_document']


def test_retry_hint_is_only_schema_guidance_from_a_rejected_docx_call():
    from bank_runtime.artifact_schema import docx_retry_schema_hint
    payload = {'artifact_type': 'docx', 'delivery_plan': {
        'target_format': 'docx', 'layout_kind': 'official_document', 'document_type': 'request'}}
    hint = docx_retry_schema_hint('artifact_generate', payload)
    assert hint.layout_kind == 'official_document'
    assert docx_retry_schema_hint('artifact_convert', payload) is None
    assert docx_retry_schema_hint('artifact_generate', {**payload, 'artifact_type': 'pptx'}) is None
    assert docx_retry_schema_hint('artifact_generate', {'artifact_type': 'docx'}) is None


@pytest.mark.asyncio
async def test_followup_without_frozen_intent_retains_rejected_official_layout():
    from types import SimpleNamespace
    from agentscope.message import TextBlock, ToolCallBlock
    from agentscope.model import ChatResponse
    from agentscope.permission import PermissionBehavior
    from bank_runtime.gateway.client import GatewayError
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
    class Client:
        async def preflight(self, *args, **kwargs):
            raise GatewayError('invalid', code='ARTIFACT_VALIDATION_FAILED', validation_hint='完整公文对象')
    middleware = BankRuntimeGatewayMiddleware(Client())
    engine = GatewayPermissionEngine(SimpleNamespace(context=None), middleware)
    payload = {'artifact_type': 'docx', 'content': 'broken', 'delivery_plan': {
        'target_format': 'docx', 'layout_kind': 'official_document', 'document_type': 'request'}}
    decision = await engine.check_permission(SimpleNamespace(name='artifact_generate'), payload)
    assert decision.behavior == PermissionBehavior.DENY
    assert middleware.artifact_intent is None
    tool = FunctionTool(artifact_generate)
    tools = [{'type': 'function', 'function': {'name': tool.name, 'parameters': tool.input_schema}}]
    async def model(**kwargs):
        content = kwargs['tools'][0]['function']['parameters']['properties']['content']
        assert content['properties']['kind']['enum'] == ['official_document']
        prompt = str(kwargs['messages'])
        assert 'kind' in prompt and '不得改成普通' in prompt
        return ChatResponse(content=[ToolCallBlock(id='correction', name='artifact_generate', input='{}')], is_last=True)
    await middleware.on_model_call(None, {'tools': tools}, model)


@pytest.mark.asyncio
async def test_rejected_proposal_never_overrides_trusted_layout_or_adds_tools():
    from agentscope.message import TextBlock, ToolCallBlock
    from agentscope.model import ChatResponse
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware
    intent = ArtifactDeliveryIntent('generate', 'docx', layout_kind='standard_document')
    middleware = BankRuntimeGatewayMiddleware(None, artifact_intent=intent)
    middleware._record_artifact_input_failure('artifact_generate', {'artifact_type': 'docx', 'delivery_plan': {
        'target_format': 'docx', 'layout_kind': 'official_document'}})
    tool = FunctionTool(artifact_generate)
    schemas = [{'function': {'name': tool.name, 'parameters': tool.input_schema}}]
    async def model(**kwargs):
        assert len(kwargs['tools']) == 1
        content = kwargs['tools'][0]['function']['parameters']['properties']['content']
        assert 'sections' in content['properties'] and 'kind' not in content['properties']
        return ChatResponse(content=[ToolCallBlock(id='correction', name='artifact_generate', input='{}')], is_last=True)
    await middleware.on_model_call(None, {'tools': schemas}, model)
    assert middleware.artifact_intent is intent
    assert not middleware._prepared
