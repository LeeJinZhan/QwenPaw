from copy import deepcopy
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentscope.tool import FunctionTool
from bank_runtime.artifact_tools import artifact_generate, ArtifactDeliveryIntent
from bank_runtime.artifact_schema import describe_docx_tools


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
