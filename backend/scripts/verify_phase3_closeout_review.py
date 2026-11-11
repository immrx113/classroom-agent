"""Independent closeout of frozen P03-01..16; no new acceptance clauses."""
import asyncio
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

import verify_phase3_frozen as supplied

OUT=supplied.ROOT.parent/'docs/classroom-agent/acceptance/evidence-phase3-closeout-20261029'
OUT.mkdir(exist_ok=True)
supplied.OUT=OUT
original_tests=supplied.tests


async def full_request_budget(current,client):
    current['scope']=None
    lecture,_=await supplied.create_lecture(supplied.SUBJECT_A,'closeout-budget')
    segments=supplied.make_long_segments(n=20,base=600)
    _,version=await supplied.add_transcript(lecture,segments)
    class CapturingModel(supplied.FakeOrganizeModel):
        def __init__(self):
            super().__init__();self.requests=[]
        async def extract_chunk(self,chunk):
            core=sum(len(s.get('content') or '') for s in chunk.core)
            context=sum(len(s.get('content') or '') for s in chunk.context)
            self.requests.append(dict(chunk=chunk.chunk_index,core=core,context=context,input=core+context))
            return await super().extract_chunk(chunk)
    model=CapturingModel()
    await supplied.organize_full(lecture,version,model)
    budget=supplied.settings.ORGANIZE_CONTEXT_BUDGET-supplied.settings.ORGANIZE_PROMPT_RESERVE-supplied.settings.ORGANIZE_OUTPUT_RESERVE
    supplied.record('P03-03','原冻结预算：实际核心加上下文均须计入输入预算',
        bool(model.requests) and all(row['input']<=budget for row in model.requests),
        dict(input_budget=budget,estimator='len(content), same as delivered chunking',requests=model.requests))


async def partial_publish(current,client):
    current.update(subject_id=supplied.SUBJECT_A,scope=None)
    lecture,_=await supplied.create_lecture(supplied.SUBJECT_A,'closeout-partial-publish')
    segments=[supplied._seg(i,'块'+str(i)+'。'*1990,start_ms=i*5000,end_ms=(i+1)*5000-100) for i in range(4)]
    _,version=await supplied.add_transcript(lecture,segments)
    model=supplied.FakeOrganizeModel();model.chunk_fail={1:'permanent'}
    jid=await supplied.organize_full(lecture,version,model)
    job=await supplied.fetch_job(jid)
    note=await supplied.fetch_note(job.output_version)
    before=dict(status=note.status,coverage=note.content['coverage'])
    response=await client.post(f'/api/notes/{note.id}/publish')
    from sqlalchemy import select,func
    async with supplied.async_session_factory() as db:
        note=await db.get(supplied.NoteVersion,note.id)
        meeting=await db.get(supplied.Meeting,lecture)
        intents=(await db.execute(select(func.count()).select_from(supplied.IndexIntent).where(supplied.IndexIntent.note_version_id==note.id))).scalar_one()
        after=dict(status=note.status,coverage=note.content['coverage'],active_note=str(meeting.active_note_version_id),intents=intents)
        ok=note.status!='published' and meeting.active_note_version_id!=note.id and intents==0
    supplied.record('P03-12','原冻结步骤：缺块草稿实际请求发布必须拒绝',ok,
        dict(job_status=job.status,http_status=response.status_code,before=before,after=after))


async def repeat_request(current,client):
    current.update(subject_id=supplied.SUBJECT_A,scope=None)
    lecture,_=await supplied.create_lecture(supplied.SUBJECT_A,'closeout-repeat-request')
    _,version=await supplied.add_transcript(lecture,[
        supplied._seg(0,'幂等事实',start_ms=0,end_ms=1000),
        supplied._seg(1,'【作业】下次课前完成第三章习题',start_ms=1100,end_ms=2000)])
    payload={'transcript_version_id':str(version)}
    first=await client.post(f'/api/lectures/{lecture}/organize',json=payload)
    second=await client.post(f'/api/lectures/{lecture}/organize',json=payload)
    import uuid
    model=supplied.FakeOrganizeModel()
    jobs=list(dict.fromkeys([uuid.UUID(first.json()['id']),uuid.UUID(second.json()['id'])]))
    for jid in jobs:await supplied.run_organize_job(jid,model)
    from sqlalchemy import select
    async with supplied.async_session_factory() as db:
        notes=list((await db.execute(select(supplied.NoteVersion).where(supplied.NoteVersion.lecture_id==lecture))).scalars())
        task_count=sum(len(n.content.get('tasks',[])) for n in notes)
        note_ids=[str(n.id) for n in notes]
    supplied.record('P03-13','原冻结步骤：同一整理业务请求重试不重复笔记和教师任务',
        len(notes)==1 and task_count==1,
        dict(job_ids=[str(j) for j in jobs],note_ids=note_ids,note_count=len(notes),teacher_task_count=task_count,extract_calls=len(model.calls)))


def tests(current,client):
    yield from original_tests(current,client)
    yield 'P03-03',full_request_budget(current,client)
    yield 'P03-12',partial_publish(current,client)
    yield 'P03-13',repeat_request(current,client)


def snapshot():
    root=supplied.ROOT.parent
    paths=[]
    for folder in ('backend/app','backend/alembic','backend/scripts','frontend/src'):
        paths.extend(p for p in (root/folder).rglob('*') if p.is_file() and p.suffix in ('.py','.ts','.tsx') and '__pycache__' not in p.parts)
    paths.append(root/'docs/classroom-agent/acceptance/phase-03-frozen-checklist-v1.md')
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


async def verify_cleanup():
    conn=await supplied.asyncpg.connect(supplied.BASE+'/postgres',timeout=5)
    try:return await conn.fetch('SELECT datname FROM pg_database WHERE datname=ANY($1::text[])',supplied.DBS)
    finally:await conn.close()


def main():
    before=snapshot()
    (OUT/'independent-source-manifest.json').write_text(json.dumps(before,indent=2),encoding='utf-8')
    supplied.tests=tests
    started=datetime.now(timezone.utc).isoformat()
    code=0
    try:asyncio.run(supplied.main())
    except SystemExit as exc:code=exc.code or 0
    finally:
        remaining=asyncio.run(verify_cleanup())
        (OUT/'run.json').write_text(json.dumps(dict(started_utc=started,finished_utc=datetime.now(timezone.utc).isoformat(),exit_code=code,source_unchanged=before==snapshot(),test_databases=supplied.DBS,remaining_databases=[r['datname'] for r in remaining]),indent=2),encoding='utf-8')
    sys.exit(code)


if __name__=='__main__':main()
