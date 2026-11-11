"""Closeout review: rerun submitted P0 tests and only frozen P0 scenarios."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import verify_phase2_frozen as submitted

OUT = submitted.ROOT.parent / 'docs/classroom-agent/acceptance/evidence-phase2-closeout-20261029'
OUT.mkdir(exist_ok=True)
submitted.OUT = OUT
original_tests = submitted.tests


async def upload_retry(current, client):
    """P02-07 explicitly includes retrying the same accepted upload operation."""
    submitted.clear_provider_override()
    fake=submitted.FakeASRProvider()
    submitted.set_provider(fake)
    current['subject_id']=submitted.SUBJECT_A
    lecture,_=await submitted.create_lecture(submitted.SUBJECT_A,'closeout-upload-retry')
    audio=submitted.make_wav(4.0)
    # Same lecture, filename and bytes, repeated accepted upload; no new-version request.
    first=await client.post(f'/api/lectures/{lecture}/audio',files={'file':('retry.wav',audio,'audio/wav')})
    second=await client.post(f'/api/lectures/{lecture}/audio',files={'file':('retry.wav',audio,'audio/wav')})
    first_ids,second_ids=first.json(),second.json()
    from app.models.transcript_version import TranscriptVersion
    from app.models.audio_asset import AudioAsset
    from sqlalchemy import select,func
    import uuid
    job_ids=[uuid.UUID(first_ids['job_id']),uuid.UUID(second_ids['job_id'])]
    for job_id in dict.fromkeys(job_ids):
        async with submitted.async_session_factory() as db:
            await submitted.run_job(db,job_id,provider=fake)
    async with submitted.async_session_factory() as db:
        asset_count=(await db.execute(select(func.count()).select_from(AudioAsset).where(AudioAsset.lecture_id==lecture))).scalar_one()
        version_count=(await db.execute(select(func.count()).select_from(TranscriptVersion).where(TranscriptVersion.lecture_id==lecture))).scalar_one()
    submitted.record('P02-07','原冻结场景：同一已接受上传请求重试不重复任务结果',
        first.status_code==201 and second.status_code in (200,201) and first_ids==second_ids and asset_count==1 and version_count==1,
        dict(first=first_ids,retry=second_ids,assets=asset_count,versions=version_count,submit_calls=fake.submit_calls))
    submitted.clear_provider_override()


async def cancel_running(current,client):
    """P02-14 specifies cancellation while running and late external results."""
    submitted.clear_provider_override()
    current['subject_id']=submitted.SUBJECT_A
    lecture,_=await submitted.create_lecture(submitted.SUBJECT_A,'closeout-running-cancel')
    fake=submitted.FakeASRProvider()
    submitted.set_provider(fake)
    async with submitted.async_session_factory() as db:
        _,job=await submitted.upload_bytes(db,lecture,'cancel.wav',submitted.make_wav(4.0))
        jid=job.id
    responses=[]
    original_poll=fake.poll
    async def delayed_poll(task_id):
        responses.append((await client.post(f'/api/jobs/{jid}/cancel')).status_code)
        responses.append((await client.post(f'/api/jobs/{jid}/cancel')).status_code)
        return await original_poll(task_id)
    fake.poll=delayed_poll
    async with submitted.async_session_factory() as db:
        await submitted.run_job(db,jid,provider=fake)
    from sqlalchemy import select,func
    async with submitted.async_session_factory() as db:
        job=await db.get(submitted.ProcessingJob,jid)
        version_count=(await db.execute(select(func.count()).select_from(submitted.TranscriptVersion).where(submitted.TranscriptVersion.lecture_id==lecture))).scalar_one()
        submitted.record('P02-14','原冻结场景：running取消、重复取消、迟到回执不发布',
            responses==[200,200] and job.status=='cancelled' and job.cancel_requested and version_count==0,
            dict(cancel_statuses=responses,status=job.status,versions=version_count))
    submitted.clear_provider_override()


def tests(current,client):
    yield from original_tests(current,client)
    yield 'P02-07',upload_retry(current,client)
    yield 'P02-14',cancel_running(current,client)


def snapshot():
    root=submitted.ROOT.parent
    paths=[]
    for folder in ('backend/app','backend/alembic','backend/scripts'):
        paths.extend(p for p in (root/folder).rglob('*.py') if '__pycache__' not in p.parts)
    paths.append(root/'docs/classroom-agent/acceptance/phase-02-frozen-checklist-v1.md')
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def main():
    before=snapshot()
    (OUT/'source-manifest.json').write_text(json.dumps(before,indent=2),encoding='utf-8')
    started=datetime.now(timezone.utc).isoformat()
    # Guard the exact cleanup target used by the supplied script.
    parent=Path(submitted.settings.UPLOAD_DIR).parent.resolve()
    expected=(submitted.ROOT/'uploads_phase2_test').resolve()
    if parent!=expected or expected.parent!=submitted.ROOT.resolve():
        raise RuntimeError('unexpected upload cleanup target')
    submitted.tests=tests
    exit_code=0
    try:
        asyncio.run(submitted.main())
    except SystemExit as exc:
        exit_code=exc.code or 0
    finally:
        (OUT/'run.json').write_text(json.dumps(dict(started_utc=started,finished_utc=datetime.now(timezone.utc).isoformat(),exit_code=exit_code,source_unchanged=before==snapshot(),test_databases=submitted.DBS,upload_directory=str(submitted.TEST_UPLOAD_DIR)),indent=2),encoding='utf-8')
    sys.exit(exit_code)


if __name__=='__main__':main()
