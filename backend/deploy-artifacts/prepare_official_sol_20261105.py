import hashlib,json,re,subprocess,sys,os
from pathlib import Path

ROOT=Path('/opt/shared-postgres/stacks/vanguard')
COMPOSE=ROOT/'compose.json'
WORK=ROOT/'model-switch-20261105'
SERVICES=['backend','celery-worker','celery-beat','resource-search-worker','telegram-growth-worker-survival-continuity','telegram-guardian-worker','reconciliation']
WORK.mkdir(mode=0o700,exist_ok=True)
os.chmod(WORK,0o700)
raw=(ROOT/'openai-credentials-20261105.txt').read_text(encoding='utf-8-sig')
keys=re.findall(r'sk-[A-Za-z0-9_-]+',raw)
urls=re.findall(r'https?://[^\s\"<>]+',raw)
assert len(keys)==1 and len(urls)==1 and urls[0].rstrip("/'")=='https://api.openai.com/v1'
NEW={'OPENAI_API_KEY':keys[0],'OPENAI_BASE_URL':'https://api.openai.com/v1','LLM_PROVIDER':'openai','LLM_MODEL':'gpt-6.1-sol','LLM_FAST_MODEL':'gpt-6.1-sol','LLM_MODEL_OVERRIDE':'gpt-6.1-sol'}

def run(args,**kw):
    return subprocess.run(args,check=True,**kw)

def private_write(path,content):
    with open(path,'w',opener=lambda p,f:os.open(p,f,0o600)) as f:f.write(content)
    os.chmod(path,0o600)

old='''                    response = await client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **({"extra_body": self.openai_extra_body} if self.openai_extra_body else {}),
                    )'''
new='''                    generation_options: dict[str, Any] = {
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    if model == "gpt-6.1-sol" and (
                        not self.base_url
                        or self._canonical_custom_endpoint(self.base_url) == "https://api.openai.com/v1"
                    ):
                        # The official Sol endpoint rejects max_tokens and custom
                        # temperatures. Preserve the requested completion budget.
                        generation_options = {"max_completion_tokens": max_tokens}
                    response = await client.chat.completions.create(
                        model=model,
                        messages=messages,
                        **generation_options,
                        **({"extra_body": self.openai_extra_body} if self.openai_extra_body else {}),
                    )'''
c=json.loads(COMPOSE.read_text())
plan={'compose_sha':hashlib.sha256(COMPOSE.read_bytes()).hexdigest(),'services':SERVICES,'images':{}}
built={}
for service in SERVICES:
    s=c['services'][service]
    container=s['container_name']
    info=json.loads(subprocess.check_output(['docker','inspect',container],text=True))[0]
    image_id=info['Image']
    tag_id=json.loads(subprocess.check_output(['docker','image','inspect',s['image']],text=True))[0]['Id']
    assert image_id==tag_id, 'Image tag moved: '+service
    if image_id not in built:
        index=len(built)+1
        context=WORK/('image-'+str(index))
        context.mkdir(exist_ok=True)
        source=subprocess.check_output(['docker','exec',container,'cat','/app/app/core/ai/llm_client.py'],text=True)
        assert source.count(old)==1, 'Unexpected client source: '+service
        (context/'llm_client.py').write_text(source.replace(old,new))
        base='local/vanguard:official-sol-base-20261105-'+str(index)
        target='local/vanguard:official-sol-20261105-'+str(index)
        run(['docker','tag',image_id,base])
        (context/'Dockerfile').write_text('FROM '+base+'\nCOPY llm_client.py /app/app/core/ai/llm_client.py\n')
        print('building',service,target,flush=True)
        run(['docker','build','--network=none','-t',target,str(context)],stdout=sys.stdout,stderr=sys.stderr)
        built[image_id]=target
    plan['images'][service]=built[image_id]
smoke_env=dict(c['services']['backend']['environment'])
smoke_env.update(NEW)
private_write(WORK/'smoke.env',''.join(k+'='+str(v)+'\n' for k,v in smoke_env.items()))
smoke='''import asyncio,sys
from app.core.ai.llm_client import LLMClient
from app.core.config import settings
async def main():
    try:
        client=LLMClient()
        result=await client._call_openai([{"role":"user","content":"Reply with exactly OK."}],settings.LLM_MODEL,0,512,max_retries=0)
        assert result.strip()=="OK"
        print("production_image_smoke: OK; model="+settings.LLM_MODEL+"; endpoint="+settings.OPENAI_BASE_URL)
    except Exception as exc:
        print("production_image_smoke_failed:",type(exc).__name__,getattr(exc,"status_code",None))
        sys.exit(1)
asyncio.run(main())
'''
run(['docker','run','--rm','--name','vanguard-openai-smoke-20261105','--network','vanguard_vanguard-internal','--env-file',str(WORK/'smoke.env'),'--entrypoint','python',plan['images']['backend'],'-c',smoke])
private_write(WORK/'plan.json',json.dumps(plan,indent=2)+'\n')
print('prepared services:',len(SERVICES),'images:',len(built),flush=True)
