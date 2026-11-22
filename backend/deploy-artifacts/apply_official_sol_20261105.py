import hashlib,json,os,re,subprocess,stat
from pathlib import Path
ROOT=Path('/opt/shared-postgres/stacks/vanguard')
WORK=ROOT/'model-switch-20261105'
COMPOSE=ROOT/'compose.json'
ENV=Path('/opt/vanguard/.env.production')
plan=json.loads((WORK/'plan.json').read_text())
assert hashlib.sha256(COMPOSE.read_bytes()).hexdigest()==plan['compose_sha'], 'Runtime configuration changed; inspect before applying'
raw=(ROOT/'openai-credentials-20261105.txt').read_text(encoding='utf-8-sig')
key=re.findall(r'sk-[A-Za-z0-9_-]+',raw)
assert len(key)==1
updates={'OPENAI_API_KEY':key[0],'OPENAI_BASE_URL':'https://api.openai.com/v1','LLM_PROVIDER':'openai','LLM_MODEL':'gpt-6.1-sol','LLM_FAST_MODEL':'gpt-6.1-sol','LLM_MODEL_OVERRIDE':'gpt-6.1-sol'}

def private_write(p,content):
    with open(p,'w',opener=lambda path,flags:os.open(path,flags,0o600)) as f:f.write(content)
    os.chmod(p,0o600)

original=COMPOSE.read_text()
config=json.loads(original)
for name in plan['services']:
    config['services'][name]['environment'].update(updates)
    config['services'][name]['image']=plan['images'][name]
staged=ROOT/'compose.official-sol-staging.json'
private_write(staged,json.dumps(config,ensure_ascii=False,indent=2)+'\n')
subprocess.run(['docker','compose','-f',str(staged),'config','--quiet'],check=True)
assert not (WORK/'compose.before.json').exists(), 'An apply has already been attempted; inspect saved state'
private_write(WORK/'compose.before.json',original)
if ENV.exists():
    original_env=ENV.read_text()
    private_write(WORK/'environment.before',original_env)
    found=set()
    lines=[]
    for line in original_env.splitlines():
        match=re.match(r'^(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=',line)
        if match and match[1] in updates:
            field=match[1]
            lines.append(field+'='+updates[field])
            found.add(field)
        else: lines.append(line)
    lines.extend(field+'='+value for field,value in updates.items() if field not in found)
    env_staged=ENV.with_name('.env.production.official-sol-staging')
    private_write(env_staged,'\n'.join(lines)+'\n')
    os.replace(env_staged,ENV)
os.replace(staged,COMPOSE)
print('runtime configuration updated for',len(plan['services']),'Vanguard services',flush=True)
subprocess.run(['docker','compose','-f',str(COMPOSE),'config','--quiet'],check=True)
subprocess.run(['docker','compose','-f',str(COMPOSE),'up','-d','--no-deps','--no-build','--pull','never','--wait','--wait-timeout','180',*plan['services']],check=True)
print('OFFICIAL_SOL_DEPLOY_COMPLETE',flush=True)
