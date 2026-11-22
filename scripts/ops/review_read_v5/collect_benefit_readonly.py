"""Host-only bounded numeric collector; no business writes or Telegram calls.

Missing historic arrivals/health/latency proof stays null. This produces a
candidate window, not an accepted comparison or a claimed saving.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

CODE = r'''
import asyncio,contextlib,fcntl,hashlib,json,math,mmap,os,struct,sys,time
from datetime import datetime,timezone
from pathlib import Path
from sqlalchemy import text
from app.core.account import read_metrics as metrics
from app.core.config import settings
from app.core.database import init_db,get_db_session,close_db
from app.modules.acquisition.review_read_flags import _FLAGS,stage_enabled,measurement_stage
from app.modules.acquisition.review_storage_budget import verify

account=int(sys.argv[1]); image=sys.argv[2]; manifest_sha=sys.argv[3]
now=time.time(); end=int(now//3600)*3600; start=end-86400

class ReadonlyMetrics(metrics.FixedReadMetrics):
 @contextlib.contextmanager
 def _open(self):
  path=self.directory/metrics.FILENAME
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
  memory=None
  try:
   fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
   info=os.fstat(fd)
   if info.st_size!=metrics.FILE_BYTES or info.st_nlink!=1: raise ValueError('numeric_source_invalid')
   memory=mmap.mmap(-1,metrics.FILE_BYTES)
   offset=0
   while offset<metrics.FILE_BYTES:
    value=os.pread(fd,min(1024*1024,metrics.FILE_BYTES-offset),offset)
    if not value: raise ValueError('numeric_source_short_read')
    memory[offset:offset+len(value)]=value; offset+=len(value)
   if memory[:8]!=metrics.MAGIC or metrics._u64(memory,8)!=1 or metrics._u64(memory,24):
    raise ValueError('numeric_source_dirty_or_invalid')
   semantics=[struct.unpack_from('<d',memory,at)[0] for at in
              (metrics.SEMANTICS_AT,metrics.BUSINESS_SEMANTICS_AT)]
   if any(not math.isfinite(value) or value<=0 for value in semantics):
    raise ValueError('numeric_source_semantics_missing')
   # snapshot may conservatively expire sources in this anonymous memory.
   # The production file is never writable and no replacement is created.
   yield memory
  finally:
   if memory is not None: memory.close()
   os.close(fd)

async def main():
 flags={name:stage_enabled(account,name) for name in _FLAGS}
 configured_roles=tuple(value.strip() for value in settings.REVIEW_READ_METRICS_EXPECTED_ROLES.split(',') if value.strip())
 fingerprint=lambda value:hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
 try:
  measured=ReadonlyMetrics(settings.REVIEW_READ_METRICS_PATH,expected_roles=configured_roles).snapshot(account,now=end,hours=24)
 except Exception as exc:
  measured={'available':False,'complete':False,'reason':type(exc).__name__,'actual_reads':None}
 storage=verify({'evidence':settings.REVIEW_EVIDENCE_STORE_PATH,'metrics':settings.REVIEW_READ_METRICS_PATH,
                 'diagnostics':settings.REVIEW_DIAGNOSTICS_PATH})
 business={key:None for key in ('healthy_seconds','backlog_start','backlog_end','oldest_age_start',
  'oldest_age_end','latency_p95_seconds','ads_attempted','ads_succeeded','ads_unknown','joins_completed',
  'failed_jobs','duplicate_sends','premature_protection_releases','storage_bytes')}
 business.update(arrivals_known=False,arrivals_by_complexity=None)
 if storage['ok']: business['storage_bytes']=storage['allocated_bytes']
 configuration=None
 await init_db(create_tables=False)
 try:
  async with get_db_session() as db:
   await db.execute(text('SET TRANSACTION READ ONLY'))
   await db.execute(text("SET LOCAL statement_timeout='5000ms'"))
   args={'account':account,'start':datetime.fromtimestamp(start,timezone.utc).replace(tzinfo=None),
         'end':datetime.fromtimestamp(end,timezone.utc).replace(tzinfo=None)}
   outbound=(await db.execute(text("SELECT count(*) AS attempted,count(*) FILTER (WHERE state='succeeded' AND message_id IS NOT NULL) AS succeeded,count(*) FILTER (WHERE state='unknown') AS unknown FROM account_outbound_attempt WHERE account_id=:account AND category='ad' AND attempted_at>=:start AND attempted_at<:end"),args)).mappings().one()
   business.update(ads_attempted=int(outbound['attempted']),ads_succeeded=int(outbound['succeeded']),ads_unknown=int(outbound['unknown']))
   joined=await db.scalar(text("SELECT count(*) FROM acquisition_auto_join_attempt WHERE account_id=:account AND status='success' AND joined_at>=:start AND joined_at<:end"),args)
   business['joins_completed']=int(joined)
   backlog=(await db.execute(text("SELECT count(*) AS pending,extract(epoch FROM (CURRENT_TIMESTAMP-min(created_at))) AS oldest_age FROM group_qualification_audit WHERE account_id=:account AND state IN ('queued','running','waiting_ai','reviewing_ai')"),args)).mappings().one()
   business.update(backlog_end=int(backlog['pending']),oldest_age_end=max(0,float(backlog['oldest_age'] or 0)))
   # The snapshot was taken now, not at the previous complete-hour endpoint.
   # Retain it separately; it cannot stand in for a missing historical sample.
   current_backlog={'at':now,'pending':business.pop('backlog_end'),'oldest_age':business.pop('oldest_age_end')}
   business.update(backlog_end=None,oldest_age_end=None)
   from app.modules.acquisition.qualification_service import policy
   configuration=fingerprint(await policy(db,fresh=True))
   await db.rollback()
 finally: await close_db()
 release={'image':image,'manifest_sha256':manifest_sha,'measurement_version':measured.get('measurement_version'),
  'expected_roles':list(configured_roles),
  'attribution_version':measured.get('attribution_version'),'completion_version':measured.get('completion_version'),
  'config_fingerprint':configuration,'flags':flags,'flags_fingerprint':fingerprint(flags)}
 window={'id':f'{account}:{start}:{end}','account_id':account,'stage':measurement_stage(account),
  'window_start':start,'window_end':end,'release':release,'metrics':measured,'business':business,
  'execution_coverage_verified':False,'unmetered_channels':None}
 print(json.dumps({'safe_benefit_window':window,'current_backlog':current_backlog,
  'collector':{'read_only':True,'numeric_file_open':'O_RDONLY','numeric_working_copy':'anonymous_memory_only',
   'telegram_calls':0,'business_writes':0,'missing_fields_are_null':True,'not_an_acceptance_result':True}}))

try: asyncio.run(main())
except Exception as exc:
 print(json.dumps({'safe_benefit_collector_failure':type(exc).__name__}))
 raise SystemExit(1)
'''

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--account',type=int,required=True)
    parser.add_argument('--directory',type=Path,default=Path(__file__).resolve().parent)
    args=parser.parse_args()
    assert args.account>0
    manifest=args.directory/'prepared-release/manifest.json'
    digest=hashlib.sha256(manifest.read_bytes()).hexdigest()
    container=json.loads(subprocess.check_output(['docker','inspect','vanguard-backend']))[0]
    result=subprocess.run(['docker','exec','vanguard-backend','python','-B','-c',CODE,str(args.account),
        container['Config']['Image'],digest],capture_output=True,text=True,timeout=40)
    found=False
    for line in result.stdout.splitlines():
        try: value=json.loads(line)
        except ValueError: continue
        if isinstance(value,dict) and ('safe_benefit_window' in value or 'safe_benefit_collector_failure' in value):
            print(json.dumps(value)); found=True
    if result.returncode or not found: raise SystemExit(1)

if __name__=='__main__': main()
