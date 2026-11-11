"""Independent phase-01 round-2 review; random disposable DBs, nonzero on regressions.

Run from backend with PYTHONPATH=.; --supplement-only skips the existing suite.
"""
import asyncio
import os
import subprocess
import sys
import uuid

os.environ['OPENAI_API_KEY'] = ''
os.environ['DASHSCOPE_API_KEY'] = ''
os.environ['PYTHONPATH'] = '.'
os.environ['PYTHONUTF8'] = '1'

import asyncpg
from app.config import settings

BASE = settings.DATABASE_URL.replace('postgresql+asyncpg://', 'postgresql://').rsplit('/', 1)[0]
PREFIX = 'yuan_review_test_' + uuid.uuid4().hex[:10] + '_'
DBS = []


async def database(name, create=False):
    assert name.startswith(PREFIX) and name in DBS
    conn = await asyncpg.connect(BASE + '/postgres')
    try:
        if create:
            await conn.execute(f'CREATE DATABASE "{name}"')
        else:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


def run(script, name):
    env = dict(os.environ, DATABASE_URL=BASE.replace('postgresql://', 'postgresql+asyncpg://') + '/' + name)
    result = subprocess.run([sys.executable, 'scripts/' + script], env=env, capture_output=True, text=True, encoding='utf-8')
    print(script, 'EXIT', result.returncode, flush=True)
    lines = result.stdout.splitlines()
    for line in lines:
        if 'TOTAL ' in line or '[FAIL]' in line:
            print(line, flush=True)
    if result.returncode:
        print(result.stdout[-6000:], result.stderr[-6000:], flush=True)
        raise RuntimeError(script)


async def supplement(name):
    # ORM imports and engine must see the isolated target, never the default DB.
    settings.DATABASE_URL = BASE.replace('postgresql://', 'postgresql+asyncpg://') + '/' + name
    from app.db.session import async_session_factory, engine
    from app.models.course import Course
    from app.models.meeting import Meeting
    from app.models.audio_asset import AudioAsset
    from app.models.transcript import Transcript
    from app.models.transcript_version import TranscriptVersion, TranscriptSegment
    from app.services.evidence_service import evidence_service
    from app.services.scope import ScopeContext
    from app.db.backfill import backfill_transcript_versions
    from sqlalchemy import select
    from fastapi import HTTPException
    scope = ScopeContext(subject_id=settings.LOCAL_SUBJECT_ID)
    failures = []
    async with async_session_factory() as db:
        course = Course(owner_id=scope.subject_id, title='Review', term='review')
        db.add(course)
        await db.flush()
        meetings = []
        for mode in ('real', None, 'real'):
            meeting = Meeting(course_id=course.id, title='Review', transcription_mode=mode)
            db.add(meeting)
            await db.flush()
            db.add(Transcript(meeting_id=meeting.id, seq_index=0, content='legacy'))
            meetings.append(meeting)
        # A legitimate failed provider version must not become active via legacy repair.
        asset = AudioAsset(lecture_id=meetings[0].id, is_demo=True, storage_key='demo.wav')
        db.add(asset)
        await db.flush()
        failed = TranscriptVersion(lecture_id=meetings[0].id, audio_asset_id=asset.id,
                                   version=2, provider='mock', status='failed')
        db.add(failed)
        await db.flush()
        db.add(TranscriptSegment(version_id=failed.id, seq_index=0, content='different new text'))
        # Unknown source with multiple versions and an existing pointer.
        unknown_versions = []
        for number in (1, 2):
            audio = AudioAsset(lecture_id=meetings[1].id, is_demo=False)
            db.add(audio)
            await db.flush()
            version = TranscriptVersion(lecture_id=meetings[1].id, audio_asset_id=audio.id,
                                        version=number, status='active')
            db.add(version)
            await db.flush()
            db.add(TranscriptSegment(version_id=version.id, seq_index=0, content='legacy'))
            unknown_versions.append(version)
        meetings[1].active_audio_id = unknown_versions[0].audio_asset_id
        meetings[1].active_transcript_version_id = unknown_versions[0].id
        # Only one trustworthy time boundary: an arbitrarily late start cannot be validated.
        timed_audio = AudioAsset(lecture_id=meetings[2].id, is_demo=False)
        db.add(timed_audio)
        await db.flush()
        timed_version = TranscriptVersion(lecture_id=meetings[2].id, audio_asset_id=timed_audio.id,
                                          version=1, status='active')
        db.add(timed_version)
        await db.flush()
        db.add(TranscriptSegment(version_id=timed_version.id, seq_index=0, content='legacy',
                                 start_ms=0, end_ms=None))
        await db.flush()
        try:
            span = await evidence_service.create_evidence(db, scope,
                transcript_version_id=timed_version.id, segment_start=0, segment_end=0,
                start_ms=500000, quote='legacy')
        except HTTPException as exc:
            if exc.status_code != 422:
                raise
            print('[PASS] G3 rejects unverifiable explicit start', flush=True)
        else:
            failures.append('G3 unverifiable explicit start accepted')
            print('[FAIL] G3 accepted unverifiable start:', span.start_ms, span.end_ms, span.is_valid, flush=True)
        report = await db.run_sync(lambda session: backfill_transcript_versions(session.connection(), scope.subject_id))
        await db.refresh(failed)
        await db.refresh(asset)
        await db.refresh(meetings[0])
        await db.refresh(meetings[1])
        print('SUPPLEMENT G2 legitimate failed version:', failed.status, 'is_demo=', asset.is_demo,
              'active_pointer=', meetings[0].active_transcript_version_id == failed.id, flush=True)
        if failed.status != 'failed' or not asset.is_demo or meetings[0].active_transcript_version_id is not None:
            failures.append('G2 legitimate failed version mutated')
            print('[FAIL] G2 legitimate product preservation', flush=True)
        print('SUPPLEMENT G2 ambiguous unknown:',
              'active_pointer_retained=', meetings[1].active_transcript_version_id is not None,
              'statuses=', list((await db.execute(select(TranscriptVersion.status).where(
                  TranscriptVersion.lecture_id == meetings[1].id))).scalars()),
              'reported_isolated=', report['unknown_source_isolated'],
              'reported_ambiguous=', report['existing_versions_ambiguous'], flush=True)
        if meetings[1].active_transcript_version_id is not None or meetings[1].active_audio_id is not None:
            failures.append('G2 ambiguous unknown source remains active')
            print('[FAIL] G2 ambiguous unknown source isolation', flush=True)
        # This transaction deliberately rolls back on context exit.
    await engine.dispose()
    return failures


def main():
    failures = []
    try:
        scripts = ['verify_fresh_migration_phase1.py', 'smoke_classroom_phase1.py',
                   'verify_e5_products_migration_phase1.py', 'verify_olddata_migration_phase1.py',
                   'verify_round2_phase1.py', 'verify_http_scope_phase1.py']
        if '--supplement-only' in sys.argv:
            scripts = scripts[:1]
        fresh = PREFIX + 'fresh'
        DBS.append(fresh)
        for i, script in enumerate(scripts):
            if i < 2:
                name = fresh
            else:
                name = PREFIX + str(i)
                DBS.append(name)
            run(script, name)
        failures = asyncio.run(supplement(fresh))
    finally:
        for name in DBS:
            asyncio.run(database(name))
            print('CLEANED', name, flush=True)
    if failures:
        print('SUPPLEMENT TOTAL 3 FAIL', len(failures), flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
