"""Independent checks restricted to phase-01-frozen-checklist-v1.md."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime, timezone

os.environ.update(OPENAI_API_KEY='', DASHSCOPE_API_KEY='', PYTHONPATH='.', PYTHONUTF8='1')
from app.config import settings
import asyncpg

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/classroom-agent/acceptance/evidence-frozen-review-20261028'
OUT.mkdir(exist_ok=True)
BASE = settings.DATABASE_URL.replace('postgresql+asyncpg://', 'postgresql://').rsplit('/', 1)[0]
PREFIX = 'yuan_frozen_test_' + uuid.uuid4().hex[:10] + '_'
DBS, RESULTS = [], []


def record(item, name, ok, detail):
    row = dict(id=item, check=name, result='通过' if ok else '不通过', detail=detail)
    RESULTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)


def target(name):
    assert name.startswith(PREFIX) and name in DBS
    return BASE.replace('postgresql://', 'postgresql+asyncpg://') + '/' + name


def process(args, name, label, expected=0):
    env = dict(os.environ, DATABASE_URL=target(name))
    result = subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, encoding='utf-8')
    (OUT / (label + '.log')).write_text(result.stdout + '\nSTDERR\n' + result.stderr, encoding='utf-8')
    print(label, 'EXIT', result.returncode, flush=True)
    for line in result.stdout.splitlines():
        if 'TOTAL' in line or '[FAIL]' in line:
            print(line, flush=True)
    if result.returncode != expected:
        raise RuntimeError(label + ': see log')
    return result


async def cleanup():
    conn = await asyncpg.connect(BASE + '/postgres', timeout=5)
    try:
        for name in DBS:
            assert name.startswith(PREFIX)
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            print('CLEANED', name, flush=True)
    finally:
        await conn.close()


async def targeted():
    from app.db.session import async_session_factory, engine
    from app.models.course import Course
    from app.models.meeting import Meeting
    from app.models.audio_asset import AudioAsset
    from app.models.transcript_version import TranscriptVersion, TranscriptSegment
    from app.models.evidence import EvidenceSpan, ArtifactEvidence
    from app.models.source_document import SourceDocument, SourceDocumentVersion
    from app.models.note_version import NoteVersion
    from app.models.summary import Summary
    from app.models.decision import Decision
    from app.models.transcript import Transcript
    from app.models.knowledge_doc import KnowledgeDocument
    from app.services.course_service import course_service
    from app.services.evidence_service import evidence_service
    from app.services.lecture_service import lecture_service
    from app.services.access import get_accessible_course, get_accessible_lecture
    from app.services.scope import ScopeContext
    from app.services.decision_graph_service import decision_graph_service
    from app.services import summary_service as summary_module
    from app.db.backfill import backfill_transcript_versions, _parse_pair_ms
    from app.main import app
    from app.api.deps import get_db, get_scope
    from httpx import AsyncClient, ASGITransport
    from sqlalchemy import select, text, func
    from sqlalchemy.exc import IntegrityError
    from fastapi import HTTPException

    scope = ScopeContext(subject_id=settings.LOCAL_SUBJECT_ID)
    async with async_session_factory() as db:
        async def overridden_db():
            yield db
        app.dependency_overrides[get_db] = overridden_db
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            r = await client.post('/api/courses', json={'title':'same-name'})
            cid = uuid.UUID(r.json()['id'])
            r2 = await client.post('/api/courses', json={'title':'same-name'})
            cid2 = uuid.UUID(r2.json()['id'])
            lectures=[]
            for c in (cid, cid, cid2):
                r = await client.post(f'/api/courses/{c}/lectures', json={'title':'frozen-lecture'})
                lectures.append(uuid.UUID(r.json()['id']))
            l1, l2, l3 = [await db.get(Meeting, i) for i in lectures]
            course = await db.get(Course, cid)
            detail = await client.get(f'/api/lectures/{l1.id}')
            listing = await client.get('/api/courses')
            record('M01-01','HTTP create/list/read and ownership',
                   course.owner_id == scope.subject_id and detail.status_code == 200
                   and l1.course_id == cid and any(i['id']==str(cid) for i in listing.json()['items']),
                   dict(owner=course.owner_id, course=str(cid), lecture=str(l1.id), status=detail.status_code))
            record('M01-15','same-name identities and list contract',
                   cid != cid2 and l1.course_id != l3.course_id and
                   all(k in listing.json() for k in ('items','total','page','page_size')),
                   dict(ids=[str(cid),str(cid2)], keys=list(listing.json())))
            foreign_c = Course(owner_id='frozen-other', title='foreign')
            db.add(foreign_c); await db.flush()
            foreign = Meeting(course_id=foreign_c.id, title='foreign')
            db.add(foreign); await db.flush()
            codes=[]
            for method, url, payload in [
                ('GET',f'/api/lectures/{foreign.id}',None),
                ('GET',f'/api/meetings/{foreign.id}',None),
                ('PATCH',f'/api/meetings/{foreign.id}',{'title':'MUST NOT CHANGE'}),
                ('DELETE',f'/api/meetings/{foreign.id}',None),
                ('GET',f'/api/meetings/{foreign.id}/audio',None),
                ('GET',f'/api/meetings/{foreign.id}/transcripts',None)]:
                response=await client.request(method,url,**({'json':payload} if payload else {}))
                codes.append((method,url,response.status_code))
            await db.refresh(foreign)
            record('M01-02','foreign HTTP routes and no mutation',
                   all(c[2]==404 for c in codes) and foreign.title=='foreign',codes)
            try:
                await get_accessible_course(db,scope,foreign_c.id)
                refused=False
            except HTTPException as exc:
                refused=exc.status_code==404
            record('M01-02','foreign course service boundary',refused,{'expected':404})

            scoped=ScopeContext(subject_id=scope.subject_id,lecture_id=l1.id)
            rows,total,counts=await course_service.list_courses(db,scoped,1,20)
            record('M01-04','lecture-scoped course count',
                   len(rows)==1 and total==1 and counts.get(cid)==1,
                   dict(scope_lecture=str(l1.id), permitted_course=str(cid), actual_count=counts.get(cid), expected_count=1))
            for label,sc,obj,fn in [
                ('course mismatch',ScopeContext(scope.subject_id,course_id=cid),cid2,get_accessible_course),
                ('lecture mismatch',scoped,l2.id,get_accessible_lecture),
                ('conflicting scope',ScopeContext(scope.subject_id,course_id=cid2,lecture_id=l1.id),cid2,get_accessible_course)]:
                try:
                    await fn(db,sc,obj); ok=False
                except HTTPException as exc:
                    ok=exc.status_code==404
                record('M01-04',label,ok,{'expected':404})

            aud=AudioAsset(lecture_id=l1.id,is_demo=False)
            db.add(aud); await db.flush()
            ver=TranscriptVersion(audio_asset_id=aud.id,lecture_id=l1.id,version=1,status='active')
            db.add(ver); await db.flush()
            seg=TranscriptSegment(version_id=ver.id,seq_index=0,content='trusted',start_ms=0,end_ms=1000)
            db.add(seg); await db.flush()
            l1.active_audio_id=aud.id; l1.active_transcript_version_id=ver.id
            await db.flush()
            _,asset_summary=await lecture_service.get_lecture(db,scope,l1.id)
            record('M01-05','valid pointer control',asset_summary['active_transcript']['id']==ver.id,{'version':str(ver.id)})
            foreign_audio=AudioAsset(lecture_id=foreign.id,is_demo=False)
            db.add(foreign_audio); await db.flush()
            l1.active_audio_id=foreign_audio.id; await db.flush()
            _,asset_summary=await lecture_service.get_lecture(db,scope,l1.id)
            record('M01-05','cross-subject audio pointer',asset_summary['active_audio'] is None,{'actual':None if asset_summary['active_audio'] is None else 'leaked'})
            l1.active_audio_id=aud.id

            for kwargs in ({}, {'transcript_version_id':ver.id,'segment_start':1,'segment_end':0}):
                try:
                    await evidence_service.create_evidence(db,scope,**kwargs); ok=False
                except HTTPException as exc: ok=exc.status_code==422
                record('M01-10','missing source / inverted range',ok,str(kwargs))
            doc=SourceDocument(course_id=cid,title='doc')
            db.add(doc); await db.flush()
            dv=SourceDocumentVersion(source_document_id=doc.id,version=1,status='active')
            db.add(dv); await db.flush()
            for kwargs in (
                {'document_version_id':dv.id},
                {'document_version_id':dv.id,'transcript_version_id':ver.id,'block_id':'b','segment_start':0,'segment_end':0}):
                try:
                    await evidence_service.create_evidence(db,scope,**kwargs); ok=False
                except HTTPException as exc: ok=exc.status_code==422
                record('M01-10','missing block / dual source',ok,str(kwargs))
            span=await evidence_service.create_evidence(db,scope,transcript_version_id=ver.id,segment_start=0,segment_end=0,quote='trusted')
            dspan=await evidence_service.create_evidence(db,scope,document_version_id=dv.id,block_id='block-1')
            constraints=[
                ("INSERT INTO transcript_segments(id,version_id,seq_index,content,start_ms,end_ms) VALUES(:id,:v,1,'x',20,10)",'ck_transcript_segment_time_order',{'v':ver.id}),
                ("INSERT INTO evidence_spans(id,transcript_version_id,segment_start,segment_end,start_ms,end_ms,is_valid) VALUES(:id,:v,0,0,-1,10,true)",'ck_evidence_span_start_nonneg',{'v':ver.id}),
                ("INSERT INTO artifact_evidence(id,artifact_type,artifact_id,evidence_id) VALUES(:id,'bogus',:a,:e)",'ck_artifact_evidence_type',{'a':uuid.uuid4(),'e':span.id})]
            for sql,expected,params in constraints:
                try:
                    async with db.begin_nested():
                        await db.execute(text(sql),dict(params,id=uuid.uuid4()))
                    ok=False; msg='accepted'
                except IntegrityError as exc:
                    msg=str(exc.orig); ok=expected in msg
                record('M01-10',expected,ok,msg.split('\n')[0])

            note=NoteVersion(lecture_id=l1.id,transcript_version_id=ver.id,pipeline_version='review')
            decision=Decision(meeting_id=l1.id,title='review',embedding=None)
            summary=Summary(meeting_id=l1.id,content='old')
            othernote=NoteVersion(lecture_id=l3.id,pipeline_version='review')
            db.add_all([note,decision,summary,othernote]); await db.flush()
            for kind,obj in [('note_version',note),('decision',decision),('summary',summary)]:
                link=await evidence_service.link_artifact(db,scope,kind,obj.id,span.id)
                record('M01-11','legal '+kind+' link',link.artifact_id==obj.id,{'link':str(link.id)})
                try:
                    await evidence_service.link_artifact(db,scope,kind,uuid.uuid4(),span.id); ok=False
                except HTTPException as exc: ok=exc.status_code==404
                record('M01-11','nonexistent '+kind,ok,{'expected':404})
            try:
                await evidence_service.link_artifact(db,scoped,'note_version',othernote.id,span.id); ok=False
            except HTTPException as exc: ok=exc.status_code==404
            record('M01-04','out-of-scope artifact rejected',ok,{'expected':404})
            await decision_graph_service.save_decisions(db,l1.id,[])
            n=(await db.execute(select(func.count()).select_from(ArtifactEvidence).where(ArtifactEvidence.artifact_type=='decision',ArtifactEvidence.artifact_id==decision.id))).scalar_one()
            record('M01-11','decision replacement unlink',n==0,{'remaining_links':n})
            db.add(Transcript(meeting_id=l1.id,seq_index=0,content='trusted')); await db.flush()
            from unittest.mock import AsyncMock, patch
            with patch.object(summary_module,'USE_HARNESS_V2',False), patch.object(summary_module.summary_service,'_run_v1_workflow',new=AsyncMock(return_value=None)):
                await summary_module.summary_service.generate_summary(db,l1.id)
            n=(await db.execute(select(func.count()).select_from(ArtifactEvidence).where(ArtifactEvidence.artifact_type=='summary',ArtifactEvidence.artifact_id==summary.id))).scalar_one()
            record('M01-11','summary replacement unlink with fake workflow',n==0,{'remaining_links':n})

            _,r14=await lecture_service.get_lecture(db,scope,l1.id)
            newver=TranscriptVersion(audio_asset_id=aud.id,lecture_id=l1.id,version=2,status='active')
            db.add(newver); await db.flush()
            l1.active_transcript_version_id=newver.id; await db.flush()
            ev=await evidence_service.get_evidence(db,scope,span.id)
            dev=await evidence_service.get_evidence(db,scope,dspan.id)
            record('M01-14','historical evidence identity',ev['source_version_id']==ver.id and not ev['is_current'] and dev['content'] is None,
                   dict(source=str(ev['source_version_id']),is_current=ev['is_current'],doc_content=dev['content']))
            seg.end_ms=None; await db.flush()
            try:
                await evidence_service.create_evidence(db,scope,transcript_version_id=ver.id,segment_start=0,segment_end=0,start_ms=500000); ok=False
            except HTTPException as exc: ok=exc.status_code==422
            record('M01-13','missing end rejects explicit start',ok,{'expected':422})
            seg.start_ms=None; seg.end_ms=1000; await db.flush()
            try:
                await evidence_service.create_evidence(db,scope,transcript_version_id=ver.id,segment_start=0,segment_end=0,end_ms=0); ok=False
            except HTTPException as exc: ok=exc.status_code==422
            record('M01-13','missing start rejects explicit end',ok,{'expected':422})
            partial=await evidence_service.create_evidence(db,scope,transcript_version_id=ver.id,segment_start=0,segment_end=0)
            record('M01-13','default partial time retained',partial.start_ms is None and partial.end_ms==1000,{'start':partial.start_ms,'end':partial.end_ms})
            db.add(KnowledgeDocument(owner_id=scope.subject_id,title='archive-zebratoken',source_type='meeting_summary',source_id=l1.id,content='zebratoken',embedding=None));await db.flush()
            await course_service.delete_course(db,scope,cid)
            codes=[]
            for url in (f'/api/meetings/{l1.id}',f'/api/lectures/{l1.id}'):
                r=await client.get(url);codes.append(r.status_code)
            r=await client.post('/api/knowledge/search',json={'query':'zebratoken','top_k':5})
            listing=await client.get('/api/knowledge/documents')
            record('M01-03','archived HTTP details and knowledge visibility',codes==[404,404] and r.json()['total']==0 and all(i['title']!='archive-zebratoken' for i in listing.json()),dict(statuses=codes,search_total=r.json()['total']))
            await course_service.delete_course(db,scope,cid)
            record('M01-16','repeat archive stable',course.archived_at is not None,{'archived':str(course.archived_at)})
        app.dependency_overrides.clear()
        # Everything above rolls back.
    record('M01-08','nonfinite source time policy',_parse_pair_ms(float('inf'),float('nan'))==(None,None,True),{'input':['inf','nan'],'output':_parse_pair_ms(float('inf'),float('nan'))})
    await engine.dispose()


async def seed_migration(name):
    conn=await asyncpg.connect(BASE+'/'+name)
    try:
        ids={k:uuid.uuid4() for k in ('course','mid_real','aud_real','ver_real','seg_real','ev_real','old_t1','mid_unknown','aud_unk','ver_unk','seg_unk','old_t2')}
        await conn.execute("INSERT INTO courses(id,owner_id,title) VALUES($1,'local-user','migration review')",ids['course'])
        await conn.execute('ALTER TABLE evidence_spans DROP CONSTRAINT ck_evidence_span_single_source')
        await conn.execute('ALTER TABLE evidence_spans ADD CONSTRAINT ck_evidence_span_single_source CHECK ((transcript_version_id IS NOT NULL) != (document_version_id IS NOT NULL))')
        for unknown in (False,True):
            mid=ids['mid_unknown' if unknown else 'mid_real']
            aid=ids['aud_unk' if unknown else 'aud_real']
            vid=ids['ver_unk' if unknown else 'ver_real']
            tid=ids['old_t2' if unknown else 'old_t1']
            sid=ids['seg_unk' if unknown else 'seg_real']
            await conn.execute("INSERT INTO meetings(id,course_id,title,status,transcription_mode) VALUES($1,$2,'legacy review','pending',$3)",mid,ids['course'],None if unknown else 'real')
            await conn.execute("INSERT INTO transcripts(id,meeting_id,seq_index,content) VALUES($1,$2,0,'legacy')",tid,mid)
            # e5 requires non-null storage_key and has no source_transcript_id column.
            await conn.execute("INSERT INTO audio_assets(id,lecture_id,storage_key,version,is_demo) VALUES($1,$2,'legacy.wav',1,$3)",aid,mid,not unknown)
            await conn.execute("INSERT INTO transcript_versions(id,audio_asset_id,lecture_id,version,status) VALUES($1,$2,$3,1,$4)",vid,aid,mid,'active' if unknown else 'ready')
            await conn.execute("INSERT INTO transcript_segments(id,version_id,seq_index,content,start_ms,end_ms) VALUES($1,$2,0,'legacy',0,1000)",sid,vid)
            if unknown:
                await conn.execute('UPDATE meetings SET active_audio_id=$1,active_transcript_version_id=$2 WHERE id=$3',aid,vid,mid)
        await conn.execute('INSERT INTO evidence_spans(id,transcript_version_id,segment_start,segment_end,is_valid) VALUES($1,$2,0,0,true)',ids['ev_real'],ids['ver_real'])
        mid,aid,vid,tid= [uuid.uuid4() for _ in range(4)]
        await conn.execute("INSERT INTO meetings(id,course_id,title,status,transcription_mode) VALUES($1,$2,'H1','pending','real')",mid,ids['course'])
        await conn.execute("INSERT INTO transcripts(id,meeting_id,seq_index,content) VALUES($1,$2,0,'legacy')",tid,mid)
        await conn.execute("INSERT INTO audio_assets(id,lecture_id,storage_key,version,is_demo) VALUES($1,$2,'demo.wav',1,true)",aid,mid)
        await conn.execute("INSERT INTO transcript_versions(id,audio_asset_id,lecture_id,version,provider,status) VALUES($1,$2,$3,2,'mock','failed')",vid,aid,mid)
        await conn.execute("INSERT INTO transcript_segments(id,version_id,seq_index,content) VALUES($1,$2,0,'different new text')",uuid.uuid4(),vid)
        aid2,vid2=uuid.uuid4(),uuid.uuid4()
        await conn.execute("INSERT INTO audio_assets(id,lecture_id,storage_key,version,is_demo) VALUES($1,$2,'legacy2.wav',2,false)",aid2,ids['mid_unknown'])
        await conn.execute("INSERT INTO transcript_versions(id,audio_asset_id,lecture_id,version,status) VALUES($1,$2,$3,2,'active')",vid2,aid2,ids['mid_unknown'])
        await conn.execute("INSERT INTO transcript_segments(id,version_id,seq_index,content) VALUES($1,$2,0,'legacy')",uuid.uuid4(),vid2)
        ids.update(h1_mid=mid,h1_aid=aid,h1_vid=vid,h2_vid2=vid2)
        return ids
    finally: await conn.close()


async def verify_migration(name,ids,start):
    conn=await asyncpg.connect(BASE+'/'+name)
    try:
        row=await conn.fetchrow('SELECT v.status,a.is_demo,m.active_audio_id,m.active_transcript_version_id FROM transcript_versions v JOIN audio_assets a ON a.id=v.audio_asset_id JOIN meetings m ON m.id=v.lecture_id WHERE v.id=$1',ids['h1_vid'])
        record('M01-07',start+' H1 actual upgrade',row['status']=='failed' and row['is_demo'] and row['active_audio_id'] is None and row['active_transcript_version_id'] is None,dict(row))
        row=await conn.fetchrow('SELECT active_audio_id,active_transcript_version_id FROM meetings WHERE id=$1',ids['mid_unknown'])
        count=await conn.fetchval('SELECT count(*) FROM transcript_versions WHERE id=ANY($1::uuid[])',[ids['ver_unk'],ids['h2_vid2']])
        record('M01-09',start+' H2 actual upgrade',all(v is None for v in row.values()) and count==2,dict(row,retained_versions=count))
        ev=await conn.fetchrow('SELECT is_valid,transcript_version_id FROM evidence_spans WHERE id=$1',ids['ev_real'])
        record('M01-07',start+' existing evidence stable',ev['is_valid'] and ev['transcript_version_id']==ids['ver_real'],dict(ev))
    finally: await conn.close()
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from app.db.backfill import backfill_transcript_versions, _sec_to_ms
    eng=create_async_engine(target(name))
    async with eng.begin() as con:
        before=(await con.execute(text('SELECT count(*) FROM transcript_versions'))).scalar_one()
        for _ in range(2):
            await con.run_sync(lambda c:backfill_transcript_versions(c,settings.LOCAL_SUBJECT_ID))
        after=(await con.execute(text('SELECT count(*) FROM transcript_versions'))).scalar_one()
        record('M01-08',start+' repeat backfill and 2.5s conversion',before==after and _sec_to_ms(2.5)==2500,dict(before=before,after=after,milliseconds=_sec_to_ms(2.5)))
    await eng.dispose()


def manifest():
    files=[]
    for folder in ('backend/app','backend/alembic','backend/scripts'):
        files += [p for p in (ROOT/folder).rglob('*.py') if '__pycache__' not in p.parts]
    files += [ROOT/'docs/classroom-agent/acceptance/phase-01-frozen-checklist-v1.md']
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}


def main():
    remaining='--remaining' in sys.argv
    if remaining:
        RESULTS.extend(json.loads((OUT/'results.json').read_text(encoding='utf-8'))['results'])
    before=manifest()
    (OUT/('source-manifest-resume.json' if remaining else 'source-manifest.json')).write_text(json.dumps(before,indent=2),encoding='utf-8')
    if remaining:
        first=json.loads((OUT/'source-manifest.json').read_text(encoding='utf-8'))
        record('M01-12','application and migration snapshot unchanged between test batches',all(before[k]==v for k,v in first.items() if k.startswith(('backend/app/','backend/alembic/'))),{})
    started=datetime.now(timezone.utc).isoformat()
    try:
        fresh=PREFIX+'fresh'
        scripts=['verify_fresh_migration_phase1.py','smoke_classroom_phase1.py','verify_e5_products_migration_phase1.py','verify_olddata_migration_phase1.py','verify_round2_phase1.py','verify_http_scope_phase1.py']
        for i,script in enumerate([] if remaining else scripts):
            name=fresh if i<2 else PREFIX+str(i)
            if name not in DBS: DBS.append(name)
            process(['scripts/'+script],name,script[:-3])
        if not remaining:
            settings.DATABASE_URL=target(fresh)
            import verify_round2_review_phase1 as previous
            failures=asyncio.run(previous.supplement(fresh))
            record('M01-07/M01-09/M01-13','previous three regressions',not failures,failures)
            asyncio.run(targeted())
        for rev in ('e5f6a7b8c9d0','d5e6f7a8b9c0'):
            name=PREFIX+rev[:2];DBS.append(name)
            # Rebuild at the chosen historical start; fixture fields document original states.
            async def reset():
                conn=await asyncpg.connect(BASE+'/postgres')
                try:
                    await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                    await conn.execute(f'CREATE DATABASE "{name}"')
                finally: await conn.close()
                conn=await asyncpg.connect(BASE+'/'+name)
                try: await conn.execute('CREATE EXTENSION vector')
                finally: await conn.close()
            asyncio.run(reset())
            process(['-m','alembic','upgrade',rev],name,'start_'+rev)
            settings.DATABASE_URL=target(name)
            ids=asyncio.run(seed_migration(name))
            process(['-m','alembic','upgrade','head'],name,'upgrade_'+rev)
            asyncio.run(verify_migration(name,ids,rev))
        # Run the refusal branch in a subprocess; it must exit before DB access.
        for script in scripts:
            env=dict(os.environ,DATABASE_URL=BASE.replace('postgresql://','postgresql+asyncpg://')+'/frozen_guard_nonproduction')
            r=subprocess.run([sys.executable,'scripts/'+script],env=env,capture_output=True,text=True,encoding='utf-8')
            record('M01-12',script+' non-test refusal',r.returncode==2,{'exit':r.returncode,'stdout':r.stdout.strip()})
        record('M01-12','source snapshot unchanged',before==manifest(),{'file_count':len(before)})
    finally:
        asyncio.run(cleanup())
        (OUT/'results.json').write_text(json.dumps(dict(started_utc=started,finished_utc=datetime.now(timezone.utc).isoformat(),databases=DBS,results=RESULTS),ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    if any(r['result']=='不通过' for r in RESULTS):sys.exit(1)


if __name__=='__main__': main()
