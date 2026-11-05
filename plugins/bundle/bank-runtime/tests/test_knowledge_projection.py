"""Selective decoding of citation fields without interpreting unrelated metadata."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bank_runtime.gateway.knowledge_sources import extract_knowledge_sources

CHUNK = {"id": "part-a", "dataset_id": "collection-a", "document_id": "doc-a",
         "document_keyword": "制度.pdf", "content": "账户需要核验。", "highlight": "<em>账户</em>需要核验。"}
EXPECTED = extract_knowledge_sources({"chunks": [CHUNK], "department_scope": {"error": None}})


def result_text(metadata='["irrelevant"]', error='null', before=False):
    scope = '"department_scope":{"doc_ids":' + metadata + ',"error":' + error + '}'
    chunks = '"chunks":' + json.dumps([CHUNK], ensure_ascii=False)
    return '{' + (scope + ',' + chunks if before else chunks + ',' + scope) + '}'


@pytest.mark.parametrize("metadata", ['["bad\nidentifier"]', r'["bad\qidentifier"]', r'["bad\uZZZZidentifier"]'])
@pytest.mark.parametrize("before", [False, True])
def test_unneeded_doc_ids_strings_are_not_decoded(metadata, before):
    text = result_text(metadata=metadata, before=before)
    with pytest.raises(json.JSONDecodeError): json.loads(text)
    assert extract_knowledge_sources(text) == EXPECTED


@pytest.mark.parametrize("field", ["content_ltks", "positions", "important_keywords", "similarity"])
def test_unused_chunk_metadata_is_not_decoded(field):
    chunk = json.dumps(CHUNK, ensure_ascii=False)[:-1] + ',' + json.dumps(field) + ':"unused\nvalue"}'
    assert extract_knowledge_sources('{"chunks":[' + chunk + ']}') == EXPECTED


@pytest.mark.parametrize("error", ['"permission failure"', '{"code":"MISSING_SCOPE"}'])
@pytest.mark.parametrize("before", [False, True])
def test_error_after_or_before_skipped_metadata_still_blocks(error, before):
    assert extract_knowledge_sources(result_text('["unused\nvalue"]', error, before))[1] == 'KNOWLEDGE_RETRIEVAL_FAILED'


@pytest.mark.parametrize("tail,expected", [
    ('"error_code":"MISSING_SCOPE"', 'MISSING_SCOPE'),
    ('"error_codes":["EMPTY_DEPARTMENT"]', 'MISSING_SCOPE'),
    ('"code":503', 'KNOWLEDGE_RETRIEVAL_FAILED'),
    ('"code":false', 'KNOWLEDGE_RETRIEVAL_FAILED'),
    ('"isError":true', 'KNOWLEDGE_RETRIEVAL_FAILED'),
])
def test_top_level_errors_cannot_be_hidden_after_chunks(tail, expected):
    text = result_text('["unused\nvalue"]')[:-1] + ',' + tail + '}'
    assert extract_knowledge_sources(text)[1] == expected


@pytest.mark.parametrize("bad", [
    '{"chunks":[{"content":"bad\nbody"}]}',
    r'{"chunks":[{"content":"bad\qbody"}]}',
    '{"chunks":[{"content":"body"}],"department_scope":{"doc_ids":["open"},"error":null}}',
    result_text() + '{"error":"must not ignore trailing response"}',
    result_text()[:-1],
    result_text()[:-1] + ',"error":"bad\nerror"}',
    result_text()[:-1] + ',"error":"failure","error":null}',
])
def test_needed_fields_unclear_boundaries_and_duplicate_errors_fail_closed(bad):
    assert extract_knowledge_sources(bad) == ([], 'KNOWLEDGE_RESULT_INVALID')


def test_json_like_words_in_metadata_are_not_promoted_to_fields():
    text = result_text(json.dumps(['quote " and slash \\ and }], "chunks": [], "error": "fake"']))
    assert extract_knowledge_sources(text) == EXPECTED


def test_nested_business_envelope_and_real_mcp_blocks():
    from mcp.types import CallToolResult, TextContent
    from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value
    text = '{"code":0,"data":' + result_text('["unused\nvalue"]') + '}'
    response = CallToolResult(content=[TextContent(type='text', text=text)])
    assert extract_knowledge_sources(_blocks_from_value(response)) == EXPECTED


def test_large_unused_doc_ids_not_in_materialized_business_object():
    from bank_runtime.gateway.knowledge_json import decode_retrieval_json
    value = {"chunks": [{**CHUNK, "positions": [[1, 2]], "content_ltks": "unused"}],
             "department_scope": {"doc_ids": ["doc-unused"] * 30000, "error": None},
             "total": 1, "unused": {"nested": "value"}}
    projected = decode_retrieval_json(json.dumps(value))
    assert projected == {"chunks": [CHUNK], "department_scope": {"error": None}}
    assert extract_knowledge_sources(json.dumps(value)) == EXPECTED


def test_document_title_fallback_and_versioned_protocol_preserved():
    source = {"schema_version":"knowledge-retrieval/v1", "status":"success", "sources":[
        {"document_id":"doc", "chunk_id":"chunk", "title":"规则", "content":"正文", "highlights":[]}]}
    assert extract_knowledge_sources(json.dumps(source)) == extract_knowledge_sources(source)
    legacy = {"chunks":[{k:v for k,v in CHUNK.items() if k!='document_keyword'}],
              "doc_aggs":{"doc-a":{"doc_name":"制度.pdf", "count":1}}}
    assert extract_knowledge_sources(json.dumps(legacy)) == EXPECTED


def test_valid_multiblock_wrappers_preserve_existing_selection():
    text = result_text('["unused\nvalue"]')
    blocks = [{"type":"text", "text":"explanatory non-JSON text"}, {"type":"text", "text":text}]
    assert extract_knowledge_sources(blocks) == EXPECTED


@pytest.mark.parametrize("text", [
    result_text()[:-1] + ',"unused":[1,,2]}',
    result_text()[:-1] + ',"unused":{"key":1,}}',
    result_text()[:-1] + ',"unused":"unfinished}',
    result_text()[:-1] + ',"unused":' + '[' * 66 + '0' + ']' * 66 + '}',
    result_text()[:-1] + ',"co\\u0064e":503,"code":0}',
])
def test_skipping_does_not_hide_ambiguous_structure_or_duplicate_escaped_keys(text):
    assert extract_knowledge_sources(text) == ([], 'KNOWLEDGE_RESULT_INVALID')


def test_valid_projected_results_equal_original_objects_across_metadata_shapes():
    import random
    randomizer = random.Random(42)
    metadata = [None, [], {}, True, 5.2, 'escaped " \\ \n } ]', {'nested':[1, {'x':'y'}]}]
    for _ in range(120):
        value = {'chunks':[{**CHUNK, 'positions':randomizer.choice(metadata)}],
                 'department_scope':{'doc_ids':randomizer.choice(metadata),'error':None},
                 'unused':randomizer.choice(metadata)}
        if randomizer.choice([False, True]): value = {'code':0,'data':value}
        assert extract_knowledge_sources(json.dumps(value)) == extract_knowledge_sources(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_error,expected", [('null','completed'), ('"denied"','failed')])
async def test_real_mcp_projection_registers_only_successful_authorized_sources(scope_error, expected):
    from types import SimpleNamespace
    from test_gateway_middleware import _Client, _DelegateEngine
    from bank_runtime.gateway.middleware import BankRuntimeGatewayMiddleware, GatewayPermissionEngine
    from agentscope.message import ToolCallBlock, ToolResultState
    from agentscope.permission import PermissionBehavior
    from agentscope.tool import ToolResponse
    from mcp.types import CallToolResult, TextContent
    from qwenpaw.drivers.adapters.agentscope_tool import _blocks_from_value

    class Client(_Client):
        async def report_result(self, call, status, duration, error='', **kwargs):
            self.events.append(('result',status,error,kwargs))
            return {'status':status,'knowledge_sources':[
                {'source_id':'ks_'+'a'*32,**row} for row in kwargs.get('knowledge_sources',[])]}

    client=Client()
    middleware=BankRuntimeGatewayMiddleware(client, knowledge_citations_enabled=True)
    engine=GatewayPermissionEngine(_DelegateEngine(PermissionBehavior.ALLOW,client.events),middleware)
    name='rag-mcp-tools-prod__search_engine_ragflow_recall_tool'
    decision=await engine.check_permission(SimpleNamespace(name=name,is_external_tool=False),{'question':'核验'})
    assert decision.behavior==PermissionBehavior.ALLOW

    async def execute(**kwargs):
        response=CallToolResult(content=[TextContent(type='text',text=result_text('["bad\nmetadata"]',scope_error))])
        yield ToolResponse(id='call',content=_blocks_from_value(response),state=ToolResultState.SUCCESS)

    responses=[item async for item in middleware.on_acting(SimpleNamespace(),
        {'tool_call':ToolCallBlock(id='call',name=name,input=json.dumps({'question':'核验'}))},execute)]
    event=client.events[-1]
    assert event[0:2]==('result',expected)
    if expected=='completed':
        assert event[3]['knowledge_sources']==EXPECTED[0]
        assert '[[kb:ks_' in responses[-1].content[0].text
        assert 'bad' not in responses[-1].content[0].text
    else:
        assert 'knowledge_sources' not in event[3]
        assert responses[-1].state==ToolResultState.ERROR
