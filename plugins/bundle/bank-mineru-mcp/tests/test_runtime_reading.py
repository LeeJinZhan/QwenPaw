"""Production routing retains current authority on cached and remote results."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import pytest
from bank_mineru_mcp.document_store import DocumentStore
from bank_mineru_mcp.structured_store import StructuredStore
from bank_mineru_mcp.spreadsheet import extract_workbook
from bank_mineru_mcp.tools import MinerUToolService, ToolContractError
from bank_runtime.sandbox.file_refs import ResolvedTaskFile
from bank_runtime.gateway.document_access import approved_document_grant, consume_document_call


def test_runtime_executor_keeps_reason_without_trusting_guest_hint():
    from bank_runtime.sandbox.executor import SandboxExecutorError
    error = SandboxExecutorError('File processing rejected', 'DOCUMENT_ARGUMENT_INVALID', argument_reason='METRIC_COLUMN')
    assert error.argument_reason == 'METRIC_COLUMN'


@pytest.mark.asyncio
async def test_http_processing_error_reaches_mcp_service_catalog(tmp_path, monkeypatch):
    import httpx
    from bank_runtime.sandbox.executor import RuntimeSandboxExecutor
    real_client=httpx.AsyncClient
    def respond(request):
        assert json.loads(request.content)['tool_name']=='document.aggregate'
        return httpx.Response(502,json={'detail':{'details':{'processing_error':'DOCUMENT_ARGUMENT_INVALID',
            'argument_reason':'METRIC_COLUMN','hint':'private guest payload'}}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw))
    executor=RuntimeSandboxExecutor('http://runtime.test','test-token',{'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()})
    class Execution:
        async def execute(self,plan):
            return await executor.process_documents(tool_call_id='call',name='aggregate',arguments={'document_ref':'ds1_ref','ops':[]},plan=plan)
    service=MinerUToolService(file_resolver=None,mineru_client=None,document_store=DocumentStore(root=tmp_path))
    service._document_sources['ds1_ref']=('fr1_source','task_001')
    service._authorize_document=lambda _:None
    service._resolve_authorized_source=lambda _:type('Source',(),{'file_id':'f','sha256':'ab'*32,'extension':'.csv'})()
    with approved_document_grant('task_001','aggregate',{'document_ref':'ds1_ref','ops':[]},execution=Execution()) as metadata:
        with consume_document_call(metadata,'aggregate',{'document_ref':'ds1_ref','ops':[]}):
            with pytest.raises(ToolContractError) as failed:
                await service.execute_structured_query('aggregate',{'document_ref':'ds1_ref','ops':[]})
    assert failed.value.argument_error['reason']=='METRIC_COLUMN'
    assert 'inventory' in failed.value.argument_error['hint']
    assert 'header_row' not in failed.value.argument_error['hint']
    assert 'private' not in str(failed.value.argument_error)


@pytest.mark.asyncio
async def test_runtime_query_translates_argument_reason_into_catalog_hint(tmp_path):
    from bank_runtime.sandbox.executor import SandboxExecutorError
    source = type('Source', (), {'file_id':'f','sha256':'ab'*32,'extension':'.csv'})()
    class Execution:
        async def execute(self, plan):
            error = SandboxExecutorError('File processing rejected', 'DOCUMENT_ARGUMENT_INVALID')
            error.argument_reason = 'METRIC_COLUMN'
            raise error
    service = MinerUToolService(file_resolver=None, mineru_client=None, document_store=DocumentStore(root=tmp_path))
    service._document_sources['ds1_ref'] = ('fr1_source', 'task_001')
    service._authorize_document = lambda _: None
    service._resolve_authorized_source = lambda _: source
    with approved_document_grant('task_001','aggregate', {'document_ref':'ds1_ref','ops':[]}, execution=Execution()) as metadata:
        with consume_document_call(metadata, 'aggregate', {'document_ref':'ds1_ref','ops':[]}):
            with pytest.raises(ToolContractError) as failed:
                await service.execute_structured_query('aggregate', {'document_ref':'ds1_ref','ops':[]})
    assert failed.value.argument_error['reason'] == 'METRIC_COLUMN'


@pytest.mark.asyncio
async def test_remote_parse_cancels_local_wait_at_source_expiry(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from bank_mineru_mcp.mineru_client import MinerUClientError
    class Client:
        cancelled = False
        async def parse(self, files, **options):
            try:
                await asyncio.sleep(30)
            finally:
                self.cancelled = True
    client = Client()
    source = SimpleNamespace(expires_at=datetime.now(timezone.utc) + timedelta(seconds=.05))
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path))
    with approved_document_grant("task_001", "parse_documents", {}) as metadata:
        with consume_document_call(metadata, "parse_documents", {}):
            with pytest.raises(MinerUClientError) as raised:
                await service._remote_parse(source)
    assert raised.value.code == "MINERU_TIMEOUT"
    assert client.cancelled


@pytest.mark.asyncio
async def test_production_table_cache_rechecks_authority_and_preserves_reference(tmp_path):
    task = tmp_path / "task_001"
    task.mkdir()
    path = task / "f.csv"
    path.write_text("amount\n10\n")
    source = ResolvedTaskFile(task_id="task_001", file_id="f", path=path,
        original_name="bank.csv", extension=".csv", media_type="text/csv", size_bytes=path.stat().st_size,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    class Resolver:
        def resolve(self, ref):
            return source

    class Execution:
        runs = 0
        validations = 0
        async def validate_sources(self, sources):
            self.validations += 1
        async def execute(self, plan):
            self.runs += 1
            job = task / ".reading-physical" / ("a" * 32)
            job.mkdir(parents=True)
            inventory = extract_workbook(path, job, stem="bank")
            import json
            (job / "inventory.json").write_text(json.dumps(inventory))
            return {"items": [{"file_id": "f", "status": "ready", "job_id": "a" * 32}]}

    execution = Execution()
    service = MinerUToolService(file_resolver=Resolver(), mineru_client=None, document_store=DocumentStore(root=tmp_path),
        structured_store=StructuredStore(root=tmp_path), execution_mode="runtime")
    arguments = {"documents": [{"file_id": "f", "file_ref": "current"}]}
    results = []
    for _ in range(2):
        with approved_document_grant("task_001", "parse_documents", arguments, execution=execution) as metadata:
            with consume_document_call(metadata, "parse_documents", arguments):
                results.append(await service.parse_documents(**arguments))
    assert execution.runs == 1
    assert execution.validations >= 2
    assert results[0]["items"][0]["document_ref"] == results[1]["items"][0]["document_ref"]
    ref = results[0]['items'][0]['document_ref']
    invalid_arguments = {'document_ref': ref, 'cursor': '3', 'limit': 5}
    with approved_document_grant('task_001', 'read_document_chunks', invalid_arguments, execution=execution) as metadata:
        with consume_document_call(metadata, 'read_document_chunks', invalid_arguments):
            with pytest.raises(ToolContractError) as invalid:
                await service.execute_structured_query('read_document_chunks', invalid_arguments)
    assert invalid.value.code == 'DOCUMENT_ARGUMENT_INVALID'
    assert execution.runs == 1  # No physical query launched for a page number.
    with approved_document_grant('task_foreign', 'read_document_chunks', invalid_arguments, execution=execution) as metadata:
        with consume_document_call(metadata, 'read_document_chunks', invalid_arguments):
            with pytest.raises(ToolContractError) as denied:
                await service.execute_structured_query('read_document_chunks', invalid_arguments)
    assert denied.value.code == 'FILE_ACCESS_DENIED'
    assert execution.runs == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('images,diagnostics', [(2, []), (0, ['native_image_count_limit'])])
async def test_office_ocr_retains_gap_when_not_all_images_exported(tmp_path, images, diagnostics):
    task = tmp_path / "task_001"
    task.mkdir()
    original = task / "f.docx"
    original.write_bytes(b"original")
    source = ResolvedTaskFile(task_id="task_001", file_id="f", path=original, original_name="bank.docx",
        extension=".docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=8, expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), sha256=hashlib.sha256(original.read_bytes()).hexdigest())
    asset = task / "image_001.png"
    asset.write_bytes(b"image")
    class Client:
        async def parse(self, files, **options):
            return {"results": {"image": {"md_content": "图片正文"}}}, {files[0].file_id: "image"}
    class Execution:
        count = 0
        async def validate_sources(self, sources):
            self.count += 1
    execution = Execution()
    service = MinerUToolService(file_resolver=None, mineru_client=Client(), document_store=DocumentStore(root=tmp_path))
    native = {"markdown": "正文", "coverage": {"image_text": "unread"},
        "source_inventory": {"images": images, "source_inventory_complete": True}, 'diagnostics':diagnostics,
        "image_assets": [{"name": asset.name, "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}]}
    with approved_document_grant("task_001", "parse_documents", {}, execution=execution) as metadata:
        with consume_document_call(metadata, "parse_documents", {}):
            result = await service._ocr_native(source, native, task)
    assert "图片正文" in result["markdown"]
    assert result["coverage"]["image_text"] == "partial"
    assert execution.count == 2
def test_processing_metadata_rejects_oversize_before_loading(tmp_path):
    import pytest
    from bank_mineru_mcp.tools import _read_processing_metadata, ToolContractError
    path = tmp_path / 'inventory.json'
    with path.open('wb') as stream:
        stream.truncate(16 * 1024**2 + 1)
    with pytest.raises(ToolContractError) as failed:
        _read_processing_metadata(path, max_bytes=16 * 1024**2)
    assert failed.value.code == 'DOCUMENT_RESULT_TOO_LARGE'
    path.write_text('{"valid":true}')
    assert _read_processing_metadata(path, max_bytes=1024) == {'valid':True}


def _office_images(tmp_path, count=7, *, first_index=1):
    task = tmp_path / "task_001"
    task.mkdir()
    path = task / "f.docx"
    path.write_bytes(b"original")
    source = ResolvedTaskFile(task_id="task_001", file_id="f", path=path, original_name="bank.docx",
        extension=".docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=8, expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assets = []
    for index in range(first_index, first_index + count):
        image = task / f"image_{index:03d}.png"
        image.write_bytes(f"image-{index}".encode())
        assets.append({"name": image.name, "sha256": hashlib.sha256(image.read_bytes()).hexdigest()})
    native = {"markdown": "正文", "coverage": {"image_text": "unread"}, "diagnostics": [],
        "source_inventory": {"images": count, "source_inventory_complete": True}, "image_assets": assets}
    return source, native, task


class _BatchClient:
    def __init__(self, *, fail_batch=None, missing_image=None):
        self.calls = []
        self.fail_batch = fail_batch
        self.missing_image = missing_image

    async def parse(self, files, **options):
        from bank_mineru_mcp.mineru_client import MinerUClientError
        self.calls.append([item.file_id for item in files])
        assert options == {"parse_method": "ocr", "language": "auto", "tables": True, "formulas": True}
        for item in files:
            assert item.sha256 == hashlib.sha256(item.path.read_bytes()).hexdigest()
        if len(self.calls) == self.fail_batch:
            raise MinerUClientError("MINERU_UNAVAILABLE", "Unavailable")
        stems = {item.file_id: "upload_" + item.file_id for item in files if item.file_id != self.missing_image}
        return {"results": {stem: {"md_content": "OCR " + file_id} for file_id, stem in stems.items()}}, stems


class _OriginalExecution:
    def __init__(self, source):
        self.source = source
        self.validations = 0
        self.revoked = False

    async def validate_sources(self, sources):
        self.validations += 1
        assert sources == [{"file_id": self.source.file_id, "sha256": self.source.sha256, "extension": ".docx"}]
        if self.revoked:
            raise ToolContractError("FILE_ACCESS_DENIED", "Source revoked")


async def _run_office_ocr(service, source, native, work, execution):
    with approved_document_grant("task_001", "parse_documents", {}, execution=execution) as metadata:
        with consume_document_call(metadata, "parse_documents", {}):
            return await service._ocr_native(source, native, work)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,missing,succeeded,failed", [(None, None, 7, 0), (2, None, 4, 3), (None, "f-img4", 6, 1)])
async def test_office_ocr_batches_preserve_success_and_report_coverage(tmp_path, failure, missing, succeeded, failed):
    source, native, work = _office_images(tmp_path)
    client = _BatchClient(fail_batch=failure, missing_image=missing)
    execution = _OriginalExecution(source)
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path), ocr_batch_size=3)
    result = await _run_office_ocr(service, source, native, work, execution)
    assert client.calls == [["f-img1", "f-img2", "f-img3"], ["f-img4", "f-img5", "f-img6"], ["f-img7"]]
    assert "正文" in result["markdown"] and "OCR f-img1" in result["markdown"] and "OCR f-img7" in result["markdown"]
    assert result["coverage"]["image_text"] == ("parsed" if succeeded == 7 else "partial")
    assert result["source_inventory"]["images_ocr"] == succeeded
    assert result["source_inventory"]["images_remaining"] == failed
    assert result["ocr_batches"] == {"batch_size": 3, "batches_total": 3, "batches_completed": 2 if failure else 3,
        "batches_failed": 1 if failure else 0, "images_exported": 7, "images_succeeded": succeeded, "images_failed": failed}
    assert execution.validations >= 6


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["expiry", "revoke", "revoke_failed_batch", "cancel"])
async def test_office_ocr_stops_later_batches_when_authority_ends(tmp_path, stop):
    import asyncio
    source, native, work = _office_images(tmp_path)
    if stop == "expiry":
        source = replace(source, expires_at=datetime.now(timezone.utc) + timedelta(seconds=.03))
    execution = _OriginalExecution(source)
    class Client(_BatchClient):
        async def parse(self, files, **options):
            result = await super().parse(files, **options)
            if stop == "expiry":
                await asyncio.sleep(.08)
            elif stop == "cancel":
                raise asyncio.CancelledError()
            else:
                execution.revoked = True
                if stop == "revoke_failed_batch":
                    from bank_mineru_mcp.mineru_client import MinerUClientError
                    raise MinerUClientError("MINERU_UNAVAILABLE", "Unavailable after revoke")
            return result
    client = Client()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path), ocr_batch_size=3)
    with pytest.raises(asyncio.CancelledError if stop == "cancel" else ToolContractError) as failed:
        await _run_office_ocr(service, source, native, work, execution)
    if stop != "cancel":
        assert failed.value.code == ("FILE_REF_EXPIRED" if stop == "expiry" else "FILE_ACCESS_DENIED")
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_office_ocr_accepts_bounded_five_digit_generated_name(tmp_path):
    source, native, work = _office_images(tmp_path, count=1, first_index=10000)
    client = _BatchClient()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path), ocr_batch_size=3)
    result = await _run_office_ocr(service, source, native, work, _OriginalExecution(source))
    assert result["source_inventory"]["images_ocr"] == 1
    assert client.calls == [["f-img10000"]]
    assert "### 原件图片 10000 OCR" in result["markdown"]


@pytest.mark.asyncio
async def test_office_ocr_preserves_original_numbers_after_skipped_images(tmp_path):
    source, native, work = _office_images(tmp_path, count=4, first_index=5)
    native["source_inventory"]["images"] = 8
    native["image_export"] = {"candidates_total": 8, "exported": 4, "skipped_unreadable": 4}
    client = _BatchClient()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path), ocr_batch_size=3)
    result = await _run_office_ocr(service, source, native, work, _OriginalExecution(source))
    assert client.calls == [["f-img5", "f-img6", "f-img7"], ["f-img8"]]
    assert "### 原件图片 5 OCR\nOCR f-img5" in result["markdown"]
    assert "### 原件图片 8 OCR\nOCR f-img8" in result["markdown"]
    assert "### 原件图片 1 OCR" not in result["markdown"]
    assert result["source_inventory"]["images_ocr"] == 4
    assert result["source_inventory"]["images_remaining"] == 4
    assert result["coverage"]["image_text"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "name", "symlink", "directory"])
async def test_office_ocr_rejects_invalid_asset_before_upload(tmp_path, damage):
    source, native, work = _office_images(tmp_path, count=1)
    asset = native["image_assets"][0]
    image = work / asset["name"]
    if damage == "hash":
        image.write_bytes(b"tampered")
    elif damage == "name":
        asset["name"] = "../f.docx"
    else:
        image.unlink()
        if damage == "symlink":
            image.symlink_to(source.path)
        else:
            image.mkdir()
    client = _BatchClient()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path), ocr_batch_size=3)
    with pytest.raises(ToolContractError):
        await _run_office_ocr(service, source, native, work, _OriginalExecution(source))
    assert client.calls == []


@pytest.mark.asyncio
async def test_office_ocr_stops_batches_at_markdown_output_budget(tmp_path):
    source, native, work = _office_images(tmp_path)
    client = _BatchClient()
    store = DocumentStore(root=tmp_path, max_document_bytes=300)
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=store, ocr_batch_size=3)
    result = await _run_office_ocr(service, source, native, work, _OriginalExecution(source))
    assert len(result["markdown"].encode("utf-8")) <= store.max_document_bytes
    assert result["markdown"].startswith("正文")
    assert len(client.calls) == 1
    assert result["coverage"]["image_text"] == "partial"
    assert result["ocr_batches"]["stop_reason"] == "output_byte_limit"
    assert result["source_inventory"]["images_remaining"] > 0
    from bank_mineru_mcp.schemas import NormalizedDocument
    from bank_mineru_mcp.normalization import _chunks
    store.write(source, NormalizedDocument("", result["markdown"], _chunks(result["markdown"], 3200)))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["MINERU_TIMEOUT", "MINERU_SUBMIT_AMBIGUOUS"])
async def test_office_ocr_stops_after_ambiguous_batch_timeout(tmp_path, error):
    import asyncio
    source, native, work = _office_images(tmp_path)
    class Client(_BatchClient):
        async def parse(self, files, **options):
            await super().parse(files, **options)
            if error == "MINERU_SUBMIT_AMBIGUOUS":
                from bank_mineru_mcp.mineru_client import MinerUClientError
                raise MinerUClientError(error, "Task submission result unknown")
            await asyncio.sleep(30)
    client = Client()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path),
        ocr_batch_size=3, parse_timeout_seconds=.03)
    result = await _run_office_ocr(service, source, native, work, _OriginalExecution(source))
    assert len(client.calls) == 1
    assert result["ocr_batches"]["stop_reason"] == error
    assert result["ocr_batches"]["images_failed"] == 3
    assert result["ocr_batches"]["images_unprocessed"] == 4
    assert result["source_inventory"]["images_remaining"] == 7
    assert result["coverage"]["image_text"] == "unread"


@pytest.mark.asyncio
@pytest.mark.parametrize("upstream", ["MINERU_TIMEOUT", "MINERU_SUBMIT_AMBIGUOUS"])
@pytest.mark.parametrize("recheck", ["network", "FILE_ACCESS_DENIED", "FILE_REF_INVALID", "FILE_REF_EXPIRED"])
async def test_office_ocr_unknown_upstream_error_survives_network_recheck_but_policy_denies(tmp_path, upstream, recheck):
    import asyncio
    import httpx
    from bank_mineru_mcp.mineru_client import MinerUClientError
    source, native, work = _office_images(tmp_path)
    class Client(_BatchClient):
        async def parse(self, files, **options):
            await super().parse(files, **options)
            if upstream == "MINERU_SUBMIT_AMBIGUOUS":
                raise MinerUClientError(upstream, "Task submission result unknown")
            await asyncio.sleep(30)
    class Execution(_OriginalExecution):
        async def validate_sources(self, sources):
            await super().validate_sources(sources)
            if self.validations > 1:
                if recheck == "network":
                    raise httpx.ConnectError("Runtime temporarily unreachable")
                raise ToolContractError(recheck, "Source rejected")
    client = Client()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path),
        ocr_batch_size=3, parse_timeout_seconds=.03)
    execution = Execution(source)
    if recheck == "network":
        result = await _run_office_ocr(service, source, native, work, execution)
        assert result["ocr_batches"]["stop_reason"] == upstream
        assert result["markdown"] == "正文"
        assert result["source_inventory"]["images_remaining"] == 7
    else:
        with pytest.raises(ToolContractError) as denied:
            await _run_office_ocr(service, source, native, work, execution)
        assert denied.value.code == recheck
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pending", "returned"])
@pytest.mark.parametrize("recheck", ["network", "FILE_ACCESS_DENIED", "cancel"])
async def test_office_ocr_started_remote_task_cannot_be_resubmitted_after_unclassified_recheck_failure(tmp_path, monkeypatch, stage, recheck):
    import asyncio
    import httpx
    real_wait = asyncio.wait
    async def fast_wait(tasks, *, timeout):
        return await real_wait(tasks, timeout=.005)
    monkeypatch.setattr("bank_mineru_mcp.tools.asyncio.wait", fast_wait)
    source, native, work = _office_images(tmp_path)
    class Client(_BatchClient):
        cancelled = False
        async def parse(self, files, **options):
            result = await super().parse(files, **options)
            if stage == "pending":
                try:
                    await asyncio.sleep(30)
                finally:
                    self.cancelled = True
            return result
    class Execution(_OriginalExecution):
        async def validate_sources(self, sources):
            await super().validate_sources(sources)
            if self.validations > 1:
                if recheck == "network":
                    raise httpx.ConnectError("Runtime temporarily unreachable")
                if recheck == "cancel":
                    raise asyncio.CancelledError()
                raise ToolContractError(recheck, "Source rejected")
    client = Client()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path), ocr_batch_size=3)
    if recheck == "network":
        result = await _run_office_ocr(service, source, native, work, Execution(source))
        assert result["ocr_batches"]["stop_reason"] == "MINERU_SUBMIT_AMBIGUOUS"
        assert result["markdown"] == "正文"
        assert result["source_inventory"]["images_remaining"] == 7
    else:
        with pytest.raises(asyncio.CancelledError if recheck == "cancel" else ToolContractError) as denied:
            await _run_office_ocr(service, source, native, work, Execution(source))
        if recheck != "cancel":
            assert denied.value.code == recheck
    assert len(client.calls) == 1
    assert client.cancelled == (stage == "pending")


@pytest.mark.asyncio
async def test_office_ocr_before_submit_network_failure_is_not_ambiguous(tmp_path):
    import httpx
    source, native, work = _office_images(tmp_path)
    class Execution(_OriginalExecution):
        async def validate_sources(self, sources):
            await super().validate_sources(sources)
            raise httpx.ConnectError("Runtime temporarily unreachable")
    client = _BatchClient()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path), ocr_batch_size=3)
    with pytest.raises(httpx.ConnectError):
        await _run_office_ocr(service, source, native, work, Execution(source))
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("complete,images,export_gap", [(False, None, False), (False, 1, False), (True, 1, True)])
async def test_office_ocr_preserves_unknown_inventory_and_export_gap(tmp_path, complete, images, export_gap):
    source, native, work = _office_images(tmp_path, count=1)
    native["source_inventory"].update(images=images, source_inventory_complete=complete)
    if export_gap:
        native["image_export"] = {"candidates_total": 2, "exported": 1, "skipped_unreadable": 1}
    service = MinerUToolService(file_resolver=None, mineru_client=_BatchClient(), document_store=DocumentStore(root=tmp_path), ocr_batch_size=3)
    result = await _run_office_ocr(service, source, native, work, _OriginalExecution(source))
    assert result["coverage"]["image_text"] == "partial"
    assert result["source_inventory"]["images_remaining"] == (None if not complete else 0)


@pytest.mark.asyncio
async def test_office_ocr_rejects_original_from_other_current_task(tmp_path):
    source, native, work = _office_images(tmp_path, count=1)
    source = replace(source, task_id="task_other")
    client = _BatchClient()
    service = MinerUToolService(file_resolver=None, mineru_client=client, document_store=DocumentStore(root=tmp_path))
    with pytest.raises(ToolContractError) as failed:
        await _run_office_ocr(service, source, native, work, _OriginalExecution(source))
    assert failed.value.code == "FILE_ACCESS_DENIED"
    assert client.calls == []


def test_column_diagnostics_do_not_prescribe_case_specific_reparse():
    from bank_mineru_mcp.aggregate_contract import argument_detail
    for reason in ('METRIC_COLUMN', 'COLUMNS'):
        detail = argument_detail(reason)
        assert detail['reason'] == reason
        assert 'inventory' in detail['hint']
        for recipe in ('header_row', 'colN', 'parse_documents', '重新解析'):
            assert recipe not in detail['hint']
