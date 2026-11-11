"""Recheck only the M01-04 issue and controls in phase-01-frozen-review-01.md."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

os.environ.update(OPENAI_API_KEY='', DASHSCOPE_API_KEY='', PYTHONPATH='.', PYTHONUTF8='1')
from app.config import settings
import asyncpg

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/classroom-agent/acceptance/evidence-frozen-count-recheck-20261028'
OUT.mkdir(exist_ok=True)
BASE = settings.DATABASE_URL.replace('postgresql+asyncpg://', 'postgresql://').rsplit('/', 1)[0]
NAME = 'yuan_m01_count_test_' + uuid.uuid4().hex[:12]
URL = BASE.replace('postgresql://', 'postgresql+asyncpg://') + '/' + NAME
RESULTS = []


def record(name, ok, detail):
    row = dict(id='M01-04', check=name, result='通过' if ok else '不通过', detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


async def verify():
    settings.DATABASE_URL = URL
    from app.db.session import async_session_factory, engine
    from app.models.course import Course
    from app.models.meeting import Meeting
    from app.services.course_service import course_service
    from app.services.scope import ScopeContext
    from app.main import app
    from app.api.deps import get_db, get_scope
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import text

    initial = None
    try:
        async with async_session_factory() as db:
            initial = (await db.execute(text('SELECT count(*) FROM meetings'))).scalar_one()
            c1 = Course(owner_id=settings.LOCAL_SUBJECT_ID, title='review C1')
            c2 = Course(owner_id=settings.LOCAL_SUBJECT_ID, title='review C2')
            db.add_all([c1,c2]); await db.flush()
            l1 = Meeting(course_id=c1.id, title='L1')
            l2 = Meeting(course_id=c1.id, title='L2')
            l3 = Meeting(course_id=c2.id, title='L3')
            db.add_all([l1,l2,l3]); await db.flush()

            cases = [
                ('original scoped reproduction', ScopeContext(settings.LOCAL_SUBJECT_ID, lecture_id=l1.id), {c1.id:1}, 1),
                ('course-only count control', ScopeContext(settings.LOCAL_SUBJECT_ID, course_id=c1.id), {c1.id:2}, 1),
                ('unrestricted count control', ScopeContext(settings.LOCAL_SUBJECT_ID), {c1.id:2,c2.id:1}, None),
                ('conflicting scope remains empty', ScopeContext(settings.LOCAL_SUBJECT_ID, course_id=c2.id,lecture_id=l1.id), {}, 0),
            ]
            for label,scope,expected,total_expected in cases:
                rows,total,counts = await course_service.list_courses(db,scope,1,100)
                actual = {str(k):v for k,v in counts.items()}
                ok = all(counts.get(k)==v for k,v in expected.items())
                if total_expected is not None:
                    ok = ok and total==total_expected and len(rows)==total_expected and set(counts)==set(expected)
                record(label,ok,dict(expected={str(k):v for k,v in expected.items()},actual=actual,total=total))

            async def overridden_db():
                yield db
            app.dependency_overrides[get_db] = overridden_db
            for scoped in (True,False):
                scope = ScopeContext(settings.LOCAL_SUBJECT_ID,lecture_id=l1.id if scoped else None)
                async def overridden_scope():
                    return scope
                app.dependency_overrides[get_scope] = overridden_scope
                async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
                    response=await client.get('/api/courses?page=1&page_size=100')
                body=response.json()
                counts={i['id']:i['lecture_count'] for i in body.get('items',[])}
                expected={str(c1.id):1} if scoped else {str(c1.id):2,str(c2.id):1}
                ok=response.status_code==200 and all(counts.get(k)==v for k,v in expected.items())
                if scoped:ok=ok and body['total']==1 and set(counts)==set(expected)
                record('HTTP scoped count' if scoped else 'HTTP unrestricted control',ok,dict(status=response.status_code,expected=expected,actual=counts,total=body.get('total')))
        async with async_session_factory() as db:
            final=(await db.execute(text('SELECT count(*) FROM meetings'))).scalar_one()
            record('fixture rollback',final==initial,dict(before=initial,after=final))
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def drop_test():
    assert NAME.startswith('yuan_m01_count_test_')
    conn=await asyncpg.connect(BASE+'/postgres',timeout=5)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{NAME}" WITH (FORCE)')
        remaining=await conn.fetchval('SELECT count(*) FROM pg_database WHERE datname=$1',NAME)
        record('test database cleanup',remaining==0,dict(database=NAME,remaining=remaining))
    finally:await conn.close()


def main():
    started=datetime.now(timezone.utc).isoformat()
    old=json.loads((ROOT/'docs/classroom-agent/acceptance/evidence-frozen-review-20261028/source-manifest-resume.json').read_text(encoding='utf-8'))
    current={}
    for folder in ('backend/app','backend/alembic'):
        for path in (ROOT/folder).rglob('*.py'):
            if '__pycache__' not in path.parts:
                current[str(path.relative_to(ROOT))]=hashlib.sha256(path.read_bytes()).hexdigest()
    baseline='docs\\classroom-agent\\acceptance\\phase-01-frozen-checklist-v1.md'
    for key in old:
        if key.replace('\\','/')=='docs/classroom-agent/acceptance/phase-01-frozen-checklist-v1.md':
            baseline=key
            current[key]=hashlib.sha256((ROOT/key).read_bytes()).hexdigest()
    old_relevant={k:v for k,v in old.items() if k.replace('\\','/').startswith(('backend/app/','backend/alembic/')) or k==baseline}
    changed=[k for k in set(current)|set(old_relevant) if current.get(k)!=old_relevant.get(k)]
    record('change scope vs previous independent review',len(changed)==1 and changed[0].replace('\\','/')=='backend/app/services/course_service.py',dict(changed=changed))
    (OUT/'source-manifest.json').write_text(json.dumps(current,indent=2),encoding='utf-8')
    try:
        env=dict(os.environ,DATABASE_URL=URL)
        result=subprocess.run([sys.executable,'scripts/verify_fresh_migration_phase1.py'],env=env,capture_output=True,text=True,encoding='utf-8')
        (OUT/'prepare-test-db.log').write_text(result.stdout+'\nSTDERR\n'+result.stderr,encoding='utf-8')
        if result.returncode:raise RuntimeError('test database preparation failed; see log')
        asyncio.run(verify())
        record('source unchanged during review',all(hashlib.sha256((ROOT/k).read_bytes()).hexdigest()==v for k,v in current.items()),{})
    finally:
        try:asyncio.run(drop_test())
        finally:
            (OUT/'results.json').write_text(json.dumps(dict(started_utc=started,finished_utc=datetime.now(timezone.utc).isoformat(),database=NAME,results=RESULTS),ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    passed=all(i['result']=='通过' for i in RESULTS)
    print('TOTAL',len(RESULTS),'PASS',sum(i['result']=='通过' for i in RESULTS),'EXIT',0 if passed else 1,flush=True)
    if not passed:sys.exit(1)


if __name__=='__main__':main()
