import asyncio
from datetime import UTC, datetime, timedelta
import io
import json
import logging
import os
from pathlib import Path
import threading

import pytest

from app.modules.acquisition import review_evidence_store as evidence
from app.modules.acquisition import review_evidence_ingest as ingest
from app.modules.acquisition import review_diagnostics as diagnostic
from app.modules.acquisition import review_storage_budget as budget
from app.modules.acquisition import review_read_runtime as runtime
from tests.unit.test_review_evidence_store import config, store, observation


def test_gap_replay_batches_once_and_new_slots_inherit_without_rewriting_old(store, monkeypatch):
    first = observation(store)
    commits = []
    real = store._commit
    monkeypatch.setattr(store, '_commit', lambda fd, index, changes: (commits.append(len(changes)), real(fd,index,changes))[-1])
    assert store.record_ingest_gap_sync('growth','first',epoch=('boot',10)).data['control_updates'] == 1
    old = store.get_observation_sync(first.handle).data
    for _ in range(100):
        assert store.record_ingest_gap_sync('growth','first',epoch=('boot',10)).data['control_updates'] == 0
    assert commits == [1]
    new = observation(store, group=-1002, message=11)
    commits.clear()
    assert store.record_ingest_gap_sync('growth','first',epoch=('boot',10)).data['control_updates'] == 1
    assert commits == [1]
    assert store.get_observation_sync(first.handle).data == old
    assert store.get_observation_sync(new.handle).data['control']['facts']['gap']


def test_new_failures_and_real_control_events_invalidate_handoff_but_stale_retry_does_not(store):
    item = observation(store, now=datetime.now(UTC)-timedelta(hours=25))
    assert store.record_ingest_gap_sync('growth','one',epoch=('boot',10)).ok
    token = store.prepare_handoff_sync(item.handle,'unknown').data['token']
    assert store.record_ingest_gap_sync('backend','independent',epoch=('boot',11)).ok
    assert store.ack_handoff_sync(item.handle,token).reason == 'handoff_token_mismatch'
    token = store.get_observation_sync(item.handle).data['control']['handoff']['token']
    assert store.record_ingest_gap_sync('growth','one',epoch=('boot',10)).data['control_updates'] == 0
    assert store.get_observation_sync(item.handle).data['control']['handoff']['token'] == token
    assert store.record_ingest_gap_sync('growth','new',epoch=('boot',20)).data['control_updates'] == 1
    assert store.ack_handoff_sync(item.handle,token).reason == 'handoff_token_mismatch'
    assert store.record_ingest_gap_sync('growth','stale',epoch=('boot',12)).data['control_updates'] == 0
    assert store.record_ingest_gap_sync('growth','restart',epoch=('newboot',1)).data['control_updates'] == 1
    for kind in ('warning','edited','deleted','gap','unknown'):
        token = store.get_observation_sync(item.handle).data['control']['handoff']['token']
        assert store.record_control_sync(item.handle,kind,source_id=17).ok
        assert store.ack_handoff_sync(item.handle,token).reason == 'handoff_token_mismatch'


@pytest.mark.asyncio
async def test_dirty_queue_burst_does_not_rewrite_each_old_reservation(store, monkeypatch):
    original = observation(store)
    worker = ingest.EvidenceIngestWorker(store)
    assert await worker.start()
    baseline = store.stats_sync().data['lifecycle_counters']['control_updates']
    monkeypatch.setattr(worker,'_capture_sync',lambda _item: True)
    worker._dirty('synthetic_failure')
    for _ in range(100):
        worker.pending.append(ingest._Pending(2,b'x',b'{}'))
        worker.pending_bytes += 3
        worker.account_pending[2] += 1
    worker.wake.set()
    await asyncio.wait_for(worker.drain(),5)
    assert store.stats_sync().data['lifecycle_counters']['control_updates'] == baseline+1
    assert store.get_observation_sync(original.handle).data['records']
    await worker.stop()


def test_history_ttl_uses_original_event_and_earliest_record_without_replay_extension(store, monkeypatch):
    now = datetime.now(UTC).timestamp()
    monkeypatch.setattr(evidence.time,'time',lambda:now)
    records = [
        {'key':'old','body':'one','metadata':{'date':now-71*3600}},
        {'key':'edited','body':'two','metadata':{'date':now-72*3600,'edit_date':now-3600}},
    ]
    saved = store.save_history_page_sync(2,-1,key='page',records=records,metadata={'scope':'one'},ttl_seconds=72*3600)
    assert saved.ok and saved.data['expires_at'] == now+3600
    assert store.load_history_page_sync(2,-1,key='page').ok
    monkeypatch.setattr(evidence.time,'time',lambda:now+600)
    replay = store.save_history_page_sync(2,-1,key='page',records=records,metadata={'scope':'one'},ttl_seconds=72*3600)
    assert replay.ok and replay.data['expires_at'] == now+3600
    monkeypatch.setattr(evidence.time,'time',lambda:now+3600)
    assert not store.load_history_page_sync(2,-1,key='page').ok


def test_default_history_compatibility_and_timestamp_failure(store, monkeypatch):
    now = datetime.now(UTC).timestamp()
    monkeypatch.setattr(evidence.time,'time',lambda:now)
    old = [{'key':'old','body':'one','metadata':{'date':now-48*3600}}]
    saved = store.save_history_page_sync(2,-1,key='default',records=old,metadata={})
    assert saved.ok and saved.data['expires_at'] == now+300
    assert store.save_history_page_sync(2,-1,key='unknown',records=[{'key':'x'}],metadata={},ttl_seconds=72*3600).reason == 'history_page_event_time_unknown'
    expired=[{'key':'x','metadata':{'date':now-72*3600-1}}]
    assert not store.save_history_page_sync(2,-1,key='expired',records=expired,metadata={},ttl_seconds=72*3600).ok
    assert not store.save_history_page_sync(2,-1,key='over',records=old,metadata={},ttl_seconds=72*3600+1).ok
    monkeypatch.setattr(evidence.time,'time',lambda:now+301)
    assert not store.load_history_page_sync(2,-1,key='default').ok


def test_legacy_manifest_is_readable_without_extending_known_original_events(store,monkeypatch):
    now=datetime.now(UTC).timestamp()
    monkeypatch.setattr(evidence.time,'time',lambda:now)
    assert store.save_history_page_sync(2,-1,key='legacy',records=[{'key':'one','metadata':{'date':now-3600}}],metadata={}).ok
    def legacy(fd,index,old=False):
        slot=next(i for i,item in enumerate(index['ordinary']) if item)
        entry=index['ordinary'][slot]
        control,data=store._load_page(fd,False,slot,entry)
        data['history:legacy:manifest']['metadata'].pop('history_page_ttl',None)
        if old:
            data['history:legacy:one']['metadata']['date']=now-73*3600
        store._commit(fd,index,[(False,slot,entry,control,data)])
        return evidence.StoreResult(True,'synthetic_legacy')
    assert store._run(legacy).ok
    assert store.load_history_page_sync(2,-1,key='legacy').ok
    assert store._run(lambda fd,index:legacy(fd,index,True)).ok
    assert not store.load_history_page_sync(2,-1,key='legacy').ok


def test_diagnostics_ring_is_numeric_bounded_and_physically_expires(tmp_path):
    cache = diagnostic.DiagnosticStore(tmp_path/'diagnostic')
    for index in range(diagnostic.SLOTS+3):
        assert cache.append('review_collection_error',counts={'stage_code':index,'body':'SECRET','account_id':999},reason='RuntimeError',now=1000)
    rows = cache.snapshot(now=1001)
    assert len(rows) == diagnostic.SLOTS and rows[0]['counts']['stage_code'] == 3
    assert cache.path.stat().st_size == diagnostic.FILE_BYTES
    assert b'SECRET' not in cache.path.read_bytes() and b'account_id' not in cache.path.read_bytes()
    assert cache.snapshot(now=1000+diagnostic.RETENTION_SECONDS) == []
    assert not any(cache.path.read_bytes()[diagnostic.HEADER_BYTES:])


def test_diagnostic_filter_never_leaks_raw_log_on_write_failure(tmp_path, monkeypatch):
    filt = diagnostic.DiagnosticLogFilter(tmp_path/'diagnostic')
    monkeypatch.setattr(filt.store,'append',lambda *_args,**_kwargs:False)
    stream=io.StringIO()
    handler=logging.StreamHandler(stream)
    handler.addFilter(filt)
    event=json.dumps({'event':'review_evidence_handoff_failed','handle':'SECRET','error':'OSError'})
    handler.handle(logging.LogRecord('probe',logging.ERROR,'',1,event,(),None))
    assert stream.getvalue() == ''
    handler.handle(logging.LogRecord('probe',logging.ERROR,'',1,'review_evidence_handoff_failed handle=SECRET',(),None))
    assert stream.getvalue() == ''


def test_interrupted_diagnostic_erase_cannot_leave_payload_beyond_retention(tmp_path):
    cache=diagnostic.DiagnosticStore(tmp_path/'diagnostic')
    assert cache.append('review_collection_error',reason='RuntimeError',now=1000)
    with cache.path.open('r+b') as stream:
        stream.seek(diagnostic.HEADER_BYTES)
        stream.write(bytes(diagnostic.FRAME.size))
    assert b'RuntimeError' in cache.path.read_bytes()
    assert cache.snapshot(now=1001) == []
    assert not any(cache.path.read_bytes()[diagnostic.HEADER_BYTES:])


def test_budget_verification_allows_uninitialized_directories_but_rejects_extra_files(tmp_path):
    paths={name:tmp_path/name for name in budget.FILES}
    assert budget.verify(paths)['ok']
    assert budget.DECLARED_PEAK_BYTES < budget.HARD_LIMIT_BYTES
    paths['evidence'].mkdir()
    (paths['evidence']/'unknown.wal').write_bytes(b'x')
    assert budget.verify(paths)['reason'] == 'unexpected_component_artifact'
    assert not budget.verify({**paths,'diagnostics':paths['metrics']})['ok']


def test_capacity_failure_disables_only_new_store_and_preserves_old_maintenance(store,monkeypatch):
    old=observation(store)
    monkeypatch.setattr(runtime,'_store',store)
    monkeypatch.setattr(runtime,'_store_path',str(store.directory))
    monkeypatch.setattr(runtime.settings,'REVIEW_EVIDENCE_STORE_PATH',str(store.directory))
    monkeypatch.setattr(runtime.settings,'REVIEW_EVIDENCE_STORE_ENABLED',True)
    monkeypatch.setattr(runtime,'enabled_for',lambda _account:True)
    monkeypatch.setattr(runtime,'component_budget_status',lambda:{'ok':False})
    assert runtime.store_for(2) is None
    assert runtime.maintenance_store(2) is store
    assert store.get_observation_sync(old.handle).ok


@pytest.mark.asyncio
async def test_new_fault_during_fsync_cannot_be_cleared_by_old_batch(store,monkeypatch):
    observation(store)
    worker=ingest.EvidenceIngestWorker(store)
    assert await worker.start()
    entered,release=threading.Event(),threading.Event()
    real=store.record_ingest_gap_sync
    calls=[]
    def blocked(source,token,*,epoch):
        calls.append(epoch)
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
        return real(source,token,epoch=epoch)
    monkeypatch.setattr(store,'record_ingest_gap_sync',blocked)
    worker._dirty('first')
    assert await asyncio.to_thread(entered.wait,2)
    worker._dirty('later')
    latest=worker.gap_order
    release.set()
    await asyncio.wait_for(worker.drain(),5)
    assert len(calls)==2 and calls[-1][1] == latest
    assert not worker.controls_pending
    await worker.stop()


def test_structlog_console_route_suppresses_raw_copy_and_preserves_other_logs(tmp_path,monkeypatch):
    import structlog
    previous=structlog.get_config()
    stream=io.StringIO()
    cache=diagnostic.configure(tmp_path/'diagnostic')
    monkeypatch.setattr(diagnostic,'_store',cache)
    try:
        structlog.configure(processors=[structlog.processors.JSONRenderer()],
            logger_factory=structlog.PrintLoggerFactory(file=stream),cache_logger_on_first_use=False)
        bound=structlog.get_logger().bind()
        diagnostic.install_logging_filters()
        bound.error('review_evidence_handoff_failed',handle='SECRET',account_id=99,error='OSError')
        assert stream.getvalue() == ''
        assert len(cache.snapshot()) == 1 and b'SECRET' not in cache.path.read_bytes()
        bound.info('original_business_log')
        assert 'original_business_log' in stream.getvalue()
    finally:
        structlog.configure(**previous)


@pytest.mark.asyncio
async def test_diagnostic_maintenance_clears_on_restart_and_runs_within_five_minutes(tmp_path,monkeypatch):
    cache=diagnostic.DiagnosticStore(tmp_path/'diagnostic')
    assert cache.append('review_collection_error',reason='RuntimeError',now=1000)
    monkeypatch.setattr(diagnostic.time,'time',lambda:1000+24*3600)
    diagnostic.configure(tmp_path/'diagnostic')
    assert not any(cache.path.read_bytes()[diagnostic.HEADER_BYTES:])
    slept=[]
    async def sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError
    monkeypatch.setattr(diagnostic.asyncio,'sleep',sleep)
    monkeypatch.setattr(diagnostic,'install_logging_filters',lambda:None)
    await diagnostic.start_maintenance()
    with pytest.raises(asyncio.CancelledError):
        await diagnostic._task
    assert slept and slept[0] <= 300
    await diagnostic.stop_maintenance()
