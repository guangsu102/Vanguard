"""Read-only post-publish checks. Never prints credentials or sends Telegram messages."""
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path('/opt/vanguard/releases/send-result-20261113-r1')

CODE = r'''
import asyncio,json,os,urllib.request,urllib.error
from sqlalchemy import text
from app.core.config import settings
from app.core.security import create_access_token
from app.core.scheduler.growth_dispatch import STAGES
import asyncpg
async def main():
 c=await asyncpg.connect(os.environ['DATABASE_URL'].replace('postgresql+asyncpg:','postgresql:'),server_settings={'default_transaction_read_only':'on','statement_timeout':'5000'})
 try:
  uid=await c.fetchval("SELECT id FROM admin_user WHERE is_active AND role='admin' ORDER BY id LIMIT 1")
  token=create_access_token({'sub':str(uid)})
  def request(path,method='GET',scrape=False):
   auth='Bearer '+(settings.METRICS_SCRAPE_TOKEN if scrape else token)
   req=urllib.request.Request('http://127.0.0.1:8000'+path,headers={'Authorization':auth,'Content-Type':'application/json'},method=method,data=b'{}' if method=='POST' else None)
   try:
    with urllib.request.urlopen(req,timeout=15) as r:
     body=r.read()
     return {'status':r.status,'body':json.loads(body) if not scrape else None,'metric_present':b'vanguard_send_result_survival_enabled 0.0' in body if scrape else None}
   except urllib.error.HTTPError as e:return {'status':e.code}
  cfg=request('/api/automation/ads/delivery-execution')
  data={'stages':list(STAGES),'health':request('/health')['status'],'settings_status':cfg['status'],
        'settings':{k:cfg.get('body',{}).get('data',{}).get(k) for k in ['send_failure_exit_threshold','send_failure_retry_seconds']},
        'survival_endpoint':request('/api/automation/ads/survival-check/run',method='POST')['status'],
        'metrics':request('/metrics',scrape=True)}
  data['accounts']=[dict(r) for r in await c.fetch("SELECT a.id,a.status,a.risk_level,c.enabled,c.auto_ads_enabled,c.auto_join_enabled FROM telegram_account a JOIN telegram_account_operation_config c ON c.account_id=a.id WHERE a.id IN (2,4,33,34,35) ORDER BY a.id")]
  data['old_review_targets']=await c.fetchval('SELECT count(*) FROM group_ad_frequency WHERE daily_review_due_at IS NOT NULL')
  data['inbox_live']=[dict(r) for r in await c.fetch("SELECT state,count(*) n FROM telegram_event_inbox WHERE state IN ('pending','processing','executing','reconciliation_required') GROUP BY state")]
  data['closed_events']=[dict(r) for r in await c.fetch("SELECT state,count(*) n FROM telegram_event_inbox WHERE state IN ('expired','archived_unknown') GROUP BY state")]
  raw=await c.fetchval("SELECT value FROM system_setting WHERE key='growth.send_result.cutover.v1'")
  state=json.loads(raw)
  data['cutover']={k:state.get(k) for k in ['started_at','resumed_at','counts','observation_blocker','observation_completed_at']}
  raw=await c.fetchval("SELECT value FROM system_setting WHERE key='growth.canary.account.2'")
  data['observation']=json.loads(raw) if raw else None
  data['bot31_absent']=not await c.fetchval('SELECT count(*) FROM telegram_account WHERE id=31')
  data['bot32']=dict(await c.fetchrow('SELECT id,is_active,status FROM telegram_account WHERE id=32'))
  print(json.dumps(data,default=str))
 finally:await c.close()
asyncio.run(main())
'''


def main():
    result = subprocess.run(['docker', 'exec', '-i', 'vanguard-backend', 'python', '-'], input=CODE,
                            text=True, capture_output=True, check=True)
    data = json.loads(result.stdout.strip().splitlines()[-1])
    infra = {}
    for name in ('sub2api-dr-postgres', 'oracle-shared-postgres-gateway', 'vanguard-redis'):
        info = json.loads(subprocess.check_output(['docker', 'inspect', name]))[0]
        infra[name] = {'id': info['Id'], 'started_at': info['State']['StartedAt'], 'restarts': info['RestartCount']}
    data['infrastructure'] = infra
    before = ROOT / 'infrastructure.before.json'
    if before.exists():
        data['infrastructure_unchanged'] = json.loads(before.read_text()) == infra
    try:
        request = urllib.request.Request('https://vanguard.pipenai.xyz/health', headers={'User-Agent':'Mozilla/5.0'})
        data['public_health'] = urllib.request.urlopen(request, timeout=20).status
    except Exception as exc:
        data['public_health'] = type(exc).__name__
    (ROOT / 'verification.json').write_text(json.dumps(data, default=str, indent=2))
    print(json.dumps(data, default=str))
    assert data['health'] == 200 and data['settings_status'] == 200
    assert data['survival_endpoint'] == 410 and data['metrics']['status'] == 200
    assert data['settings']['send_failure_exit_threshold'] == 3
    assert data['old_review_targets'] == 0 and data['infrastructure_unchanged']


if __name__ == '__main__':
    main()
