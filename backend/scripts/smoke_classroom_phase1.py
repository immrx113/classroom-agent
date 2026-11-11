r"""阶段一验收冒烟测试（断言 + 非零退出 + 测试库保护）

针对独立测试库运行（库名必须包含 ``test``，否则拒绝启动），
覆盖验收 R1—R8 的回归检查：
- R1 旧入口跨主体隔离（access.get_accessible_lecture 统一 404）
- R2 归档后详情/证据/新建课时均不可访问
- R3 跨课时 / 悬空有效指针降级为 None
- R6 ScopeContext 的 course_id / lecture_id 约束
- R7 非法时间 / 缺失定位 / 产物白名单（DB 约束 + 服务校验）
- R8 证据返回可解析来源身份
- R4/R5 旧转写回填去重、活动指针、is_demo、缺失音频（同步回填）
- G3 显式回听时间必须落在源片段时间范围（越界拒绝 / 合法裁剪 / 源时间缺失）
- G4 删除课时清理产物端证据关联（跨课时 + 同课时）
- G5 归档知识普通删除被拒绝

服务层用例不提交（事务回滚）；回填用例在独立事务内执行并清理。
运行方式：
  cd backend
  $env:PYTHONPATH="."; $env:PYTHONUTF8="1"
  python scripts/smoke_classroom_phase1.py
"""

import asyncio
import sys
import uuid

from fastapi import HTTPException
from sqlalchemy import text, select, func
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db.session import async_session_factory, engine
from app.models.audio_asset import AudioAsset
from app.models.course import Course
from app.models.decision import Decision
from app.models.evidence import EvidenceSpan, ArtifactEvidence
from app.models.knowledge_doc import KnowledgeDocument
from app.models.meeting import Meeting
from app.models.summary import Summary
from app.models.transcript_version import TranscriptVersion, TranscriptSegment
from app.schemas.course import CourseCreate, LectureCreate
from app.services.access import get_accessible_course, get_accessible_lecture
from app.services.course_service import course_service
from app.services.evidence_service import evidence_service
from app.services.knowledge_service import knowledge_service
from app.services.lecture_service import lecture_service
from app.services.meeting_service import meeting_service
from app.services.scope import ScopeContext

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def _dbname() -> str:
    return settings.DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]


def check_test_target() -> None:
    db = _dbname()
    if "test" not in db:
        print(f"REFUSE: DATABASE_URL 指向非测试库 '{db}'（库名需包含 test）")
        print("请设置 DATABASE_URL 到独立测试库后重试。")
        sys.exit(2)


async def expect_404(awaitable, name: str) -> None:
    try:
        await awaitable
    except HTTPException as e:
        record(name, e.status_code == 404, f"got {e.status_code}: {e.detail}")
    except Exception as e:  # noqa: BLE001
        record(name, False, f"unexpected {type(e).__name__}: {e}")
    else:
        record(name, False, "no exception (expected 404)")


async def phase_services() -> None:
    """服务层：R1 / R2 / R3 / R6 / R7(写校验) / R8"""
    subject_a = ScopeContext(subject_id="local-user")
    subject_b = ScopeContext(subject_id="other-user")

    async with async_session_factory() as db:
        course = await course_service.create_course(
            db, subject_a, CourseCreate(title="数据结构", teacher="王老师", term="2026秋")
        )
        lecture = await course_service.create_lecture(
            db, subject_a, course.id, LectureCreate(title="第一讲 绪论")
        )
        other_lecture = await course_service.create_lecture(
            db, subject_a, course.id, LectureCreate(title="第二讲")
        )

        # ── R1 旧入口跨主体隔离 ────────────────────────────────
        await expect_404(
            get_accessible_lecture(db, subject_b, lecture.id),
            "R1 旧入口跨主体读取拒绝",
        )

        # ── R6 ScopeContext 范围约束 ───────────────────────────
        wrong_course = Course(owner_id="local-user", title="别的课")
        db.add(wrong_course)
        await db.flush()
        await expect_404(
            get_accessible_lecture(
                db, ScopeContext(subject_id="local-user", course_id=wrong_course.id), lecture.id
            ),
            "R6 非空 course_id 不匹配拒绝",
        )
        await expect_404(
            get_accessible_lecture(
                db, ScopeContext(subject_id="local-user", lecture_id=other_lecture.id), lecture.id
            ),
            "R6 非空 lecture_id 不匹配拒绝",
        )

        # ── F5 课时范围下课程访问不得跨课 ───────────────────────
        await expect_404(
            get_accessible_course(
                db,
                ScopeContext(subject_id="local-user", lecture_id=lecture.id),
                wrong_course.id,
            ),
            "F5 课时范围下访问不含该课时的课程拒绝",
        )

        # ── R3 有效指针归属校验 ───────────────────────────────
        audio_b = AudioAsset(lecture_id=other_lecture.id, storage_key=None, is_demo=False)
        db.add(audio_b)
        await db.flush()
        lecture.active_audio_id = audio_b.id  # 跨课时指针
        await db.flush()
        _, summary = await lecture_service.get_lecture(db, subject_a, lecture.id)
        record("R3 跨课时音频指针降级", summary["active_audio"] is None)

        # 悬空指针
        lecture.active_audio_id = uuid.uuid4()
        lecture.active_transcript_version_id = uuid.uuid4()
        await db.flush()
        _, summary = await lecture_service.get_lecture(db, subject_a, lecture.id)
        record(
            "R3 悬空指针降级",
            summary["active_audio"] is None and summary["active_transcript"] is None,
        )

        # 转写-音频不一致：活动音频为 audio1，但活动转写指向 audio2
        lecture.active_audio_id = None
        lecture.active_transcript_version_id = None
        await db.flush()
        audio1 = AudioAsset(lecture_id=lecture.id, storage_key=None, is_demo=False)
        audio2 = AudioAsset(lecture_id=lecture.id, storage_key=None, is_demo=False)
        db.add_all([audio1, audio2])
        await db.flush()
        ver_mismatch = TranscriptVersion(
            audio_asset_id=audio2.id, lecture_id=lecture.id, version=1, status="active"
        )
        db.add(ver_mismatch)
        await db.flush()
        lecture.active_audio_id = audio1.id
        lecture.active_transcript_version_id = ver_mismatch.id
        await db.flush()
        _, summary = await lecture_service.get_lecture(db, subject_a, lecture.id)
        record(
            "R3 转写-音频不一致降级",
            summary["active_audio"] is not None and summary["active_transcript"] is None,
        )

        # ── R8 证据来源身份（重建一个可信链） ──────────────────
        lecture.active_audio_id = None
        lecture.active_transcript_version_id = None
        await db.flush()
        audio = AudioAsset(lecture_id=lecture.id, storage_key=None, is_demo=False)
        db.add(audio)
        await db.flush()
        version = TranscriptVersion(
            audio_asset_id=audio.id, lecture_id=lecture.id, version=1, status="active"
        )
        db.add(version)
        await db.flush()
        for seq, sms, ems in [(0, 0, 1000), (1, 1000, 2000)]:
            db.add(
                TranscriptSegment(
                    version_id=version.id, seq_index=seq, speaker="老师",
                    content=f"片段{seq}", start_ms=sms, end_ms=ems,
                )
            )
        await db.flush()
        lecture.active_audio_id = audio.id
        lecture.active_transcript_version_id = version.id
        await db.flush()

        span = EvidenceSpan(
            transcript_version_id=version.id, segment_start=0, segment_end=1,
            start_ms=0, end_ms=2000, quote="片段0 片段1", is_valid=True,
        )
        db.add(span)
        await db.flush()

        ev = await evidence_service.get_evidence(db, subject_a, span.id)
        ok = (
            ev["source_type"] == "transcript"
            and ev["source_version_id"] == version.id
            and ev["source_version"] == 1
            and ev["source_lecture_id"] == lecture.id
            and ev["source_audio_asset_id"] == audio.id
            and ev["source_course_id"] == course.id
            and ev["is_current"] is True
            and ev["content"] == ["片段0", "片段1"]
        )
        record("R8 证据返回可解析来源身份", ok, f"source_version_id={ev.get('source_version_id')}")

        # ── R7 写入校验（服务层） ─────────────────────────────
        try:
            await evidence_service.create_evidence(
                db, subject_a, transcript_version_id=version.id
            )
            record("R7 缺失片段定位拒绝(写校验)", False, "no exception")
        except HTTPException as e:
            record("R7 缺失片段定位拒绝(写校验)", e.status_code == 422, f"got {e.status_code}")

        try:
            await evidence_service.link_artifact(
                db, subject_a, "bogus_type", uuid.uuid4(), span.id
            )
            record("R7 非法产物类型拒绝", False, "no exception")
        except HTTPException as e:
            record("R7 非法产物类型拒绝", e.status_code == 422, f"got {e.status_code}")

        try:
            await evidence_service.link_artifact(
                db, subject_a, "note_version", uuid.uuid4(), span.id
            )
            record("R7 孤儿产物关联拒绝", False, "no exception")
        except HTTPException as e:
            record("R7 孤儿产物关联拒绝", e.status_code == 404, f"got {e.status_code}")

        # ── F6 证据写入校验（非负序号 / 端点存在 / quote 匹配 / 时间推导） ──
        try:
            await evidence_service.create_evidence(
                db, subject_a, transcript_version_id=version.id,
                segment_start=-1, segment_end=0,
            )
            record("F6 负片段序号拒绝", False, "no exception")
        except HTTPException as e:
            record("F6 负片段序号拒绝", e.status_code == 422, f"got {e.status_code}")

        try:
            await evidence_service.create_evidence(
                db, subject_a, transcript_version_id=version.id,
                segment_start=0, segment_end=5,
            )
            record("F6 端点不存在拒绝", False, "no exception")
        except HTTPException as e:
            record("F6 端点不存在拒绝", e.status_code == 422, f"got {e.status_code}")

        try:
            await evidence_service.create_evidence(
                db, subject_a, transcript_version_id=version.id,
                segment_start=0, segment_end=1, quote="不存在的引用文本",
            )
            record("F6 quote 与正文不符拒绝", False, "no exception")
        except HTTPException as e:
            record("F6 quote 与正文不符拒绝", e.status_code == 422, f"got {e.status_code}")

        derived = await evidence_service.create_evidence(
            db, subject_a, transcript_version_id=version.id,
            segment_start=0, segment_end=1, quote="片段0",
        )
        record(
            "F6 时间推导自片段 + 合法 quote 通过",
            derived.start_ms == 0 and derived.end_ms == 2000 and derived.is_valid is True,
            f"start_ms={derived.start_ms} end_ms={derived.end_ms}",
        )

        # ── G3 显式回听时间必须落在源片段时间范围 ──────────────
        # version 片段：seq 0 (0–1000)，seq 1 (1000–2000)
        try:
            await evidence_service.create_evidence(
                db, subject_a, transcript_version_id=version.id,
                segment_start=1, segment_end=1, start_ms=500, end_ms=1500,
                quote="片段1",
            )
            record("G3 显式 start_ms 早于源片段起点拒绝", False, "no exception")
        except HTTPException as e:
            record("G3 显式 start_ms 早于源片段起点拒绝", e.status_code == 422, f"got {e.status_code}")

        try:
            await evidence_service.create_evidence(
                db, subject_a, transcript_version_id=version.id,
                segment_start=1, segment_end=1, start_ms=1500, end_ms=2500,
                quote="片段1",
            )
            record("G3 显式 end_ms 晚于源片段终点拒绝", False, "no exception")
        except HTTPException as e:
            record("G3 显式 end_ms 晚于源片段终点拒绝", e.status_code == 422, f"got {e.status_code}")

        clip = await evidence_service.create_evidence(
            db, subject_a, transcript_version_id=version.id,
            segment_start=1, segment_end=1, start_ms=1200, end_ms=1800,
            quote="片段1",
        )
        record(
            "G3 显式合法裁剪通过",
            clip.start_ms == 1200 and clip.end_ms == 1800 and clip.is_valid is True,
            f"start_ms={clip.start_ms} end_ms={clip.end_ms}",
        )

        # 源片段无可靠时间：显式时间无法验证 → 拒绝
        audio_null = AudioAsset(lecture_id=lecture.id, storage_key=None, is_demo=False)
        db.add(audio_null)
        await db.flush()
        version_null = TranscriptVersion(
            audio_asset_id=audio_null.id, lecture_id=lecture.id, version=1, status="active"
        )
        db.add(version_null)
        await db.flush()
        db.add(
            TranscriptSegment(
                version_id=version_null.id, seq_index=0, speaker="老师",
                content="无时间片段", start_ms=None, end_ms=None,
            )
        )
        await db.flush()
        try:
            await evidence_service.create_evidence(
                db, subject_a, transcript_version_id=version_null.id,
                segment_start=0, segment_end=0, start_ms=500, end_ms=800,
                quote="无时间片段",
            )
            record("G3 源时间缺失显式时间拒绝", False, "no exception")
        except HTTPException as e:
            record("G3 源时间缺失显式时间拒绝", e.status_code == 422, f"got {e.status_code}")

        # ── R2 归档后不可访问 ────────────────────────────────
        await course_service.delete_course(db, subject_a, course.id)
        await expect_404(
            get_accessible_course(db, subject_a, course.id),
            "R2 归档后课程详情拒绝",
        )
        await expect_404(
            get_accessible_lecture(db, subject_a, lecture.id),
            "R2 归档后课时详情拒绝",
        )
        await expect_404(
            evidence_service.get_evidence(db, subject_a, span.id),
            "R2 归档后证据读取拒绝",
        )
        await expect_404(
            course_service.create_lecture(db, subject_a, course.id, LectureCreate(title="归档后")),
            "R2 归档后新建课时拒绝",
        )
        # 重复删除稳定（不抛异常）
        try:
            await course_service.delete_course(db, subject_a, course.id)
            record("R2 重复删除稳定", True)
        except Exception as e:  # noqa: BLE001
            record("R2 重复删除稳定", False, f"{type(e).__name__}: {e}")

    # 不 commit：会话关闭即回滚


async def phase_db_constraints() -> None:
    """数据库约束：R7 非法时间 / 缺失定位 / 产物白名单"""
    async with async_session_factory() as db:
        course = Course(owner_id="local-user", title="约束测试")
        db.add(course)
        await db.flush()
        meeting = Meeting(course_id=course.id, title="m", status="pending")
        db.add(meeting)
        await db.flush()
        audio = AudioAsset(lecture_id=meeting.id, storage_key=None)
        db.add(audio)
        await db.flush()
        version = TranscriptVersion(
            audio_asset_id=audio.id, lecture_id=meeting.id, status="active"
        )
        db.add(version)
        await db.flush()

        # 非法时间（负 end_ms）
        try:
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO transcript_segments "
                        "(id, version_id, seq_index, content, start_ms, end_ms) "
                        "VALUES (:id, :v, 0, 'x', 500, -100)"
                    ),
                    {"id": uuid.uuid4(), "v": version.id},
                )
            record("R7 非法时间 DB 约束", False, "no IntegrityError")
        except IntegrityError:
            record("R7 非法时间 DB 约束", True)

        # 缺失片段定位（transcript 来源无 segment 范围）
        try:
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO evidence_spans "
                        "(id, transcript_version_id, is_valid) "
                        "VALUES (:id, :v, TRUE)"
                    ),
                    {"id": uuid.uuid4(), "v": version.id},
                )
            record("R7 缺失定位 DB 约束", False, "no IntegrityError")
        except IntegrityError:
            record("R7 缺失定位 DB 约束", True)

        # 产物类型白名单
        try:
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO artifact_evidence "
                        "(id, artifact_type, artifact_id, evidence_id) "
                        "VALUES (:id, 'bogus', :a, :e)"
                    ),
                    {"id": uuid.uuid4(), "a": uuid.uuid4(), "e": uuid.uuid4()},
                )
            record("R7 产物类型白名单 DB 约束", False, "no IntegrityError")
        except IntegrityError:
            record("R7 产物类型白名单 DB 约束", True)

    # 不 commit：回滚


async def phase_backfill() -> None:
    """R4 / R5：旧转写回填去重、活动指针、is_demo、缺失音频（run_sync 事务）"""
    from app.db.backfill import backfill_legacy_course, backfill_transcript_versions

    def _sync(conn):
        mid_mock = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO meetings (id, title, status, transcription_mode, audio_url) "
                "VALUES (:i, 'mock', 'pending', 'mock', NULL)"
            ),
            {"i": mid_mock},
        )
        # 重复 seq_index=0 + 负时间片段
        conn.execute(
            text(
                "INSERT INTO transcripts "
                "(id, meeting_id, speaker, content, start_time, end_time, seq_index) "
                "VALUES (:id, :m, 'A', 'first', 0.0, 1.0, 0)"
            ),
            {"id": uuid.uuid4(), "m": mid_mock},
        )
        conn.execute(
            text(
                "INSERT INTO transcripts "
                "(id, meeting_id, speaker, content, start_time, end_time, seq_index) "
                "VALUES (:id, :m, 'A', 'dup', 0.0, 1.0, 0)"
            ),
            {"id": uuid.uuid4(), "m": mid_mock},
        )
        conn.execute(
            text(
                "INSERT INTO transcripts "
                "(id, meeting_id, speaker, content, start_time, end_time, seq_index) "
                "VALUES (:id, :m, 'A', 'negtime', -1.0, 2.0, 1)"
            ),
            {"id": uuid.uuid4(), "m": mid_mock},
        )

        mid_unknown = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO meetings (id, title, status, transcription_mode) "
                "VALUES (:i, 'unknown', 'pending', NULL)"
            ),
            {"i": mid_unknown},
        )
        conn.execute(
            text(
                "INSERT INTO transcripts "
                "(id, meeting_id, speaker, content, seq_index) "
                "VALUES (:id, :m, 'A', 'unk', 0)"
            ),
            {"id": uuid.uuid4(), "m": mid_unknown},
        )

        # 倒置时间片段（start > end）：应成对清空，保留正文
        mid_inv = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO meetings (id, title, status, transcription_mode) "
                "VALUES (:i, 'inv', 'pending', 'mock')"
            ),
            {"i": mid_inv},
        )
        conn.execute(
            text(
                "INSERT INTO transcripts "
                "(id, meeting_id, speaker, content, start_time, end_time, seq_index) "
                "VALUES (:id, :m, 'A', 'inv', 5.0, 2.0, 0)"
            ),
            {"id": uuid.uuid4(), "m": mid_inv},
        )

        backfill_legacy_course(conn, "local-user")
        report = backfill_transcript_versions(conn, "local-user")

        record("R4 重复序号去重", report["segments_dropped_duplicate_seq"] == 1, str(report))
        record("R5 缺失音频显式状态", report["missing_audio_meetings"] >= 1, str(report))
        record("R5 未知来源隔离计数", report["unknown_source_isolated"] == 1, str(report))

        mrow = conn.execute(
            text(
                "SELECT active_audio_id, active_transcript_version_id "
                "FROM meetings WHERE id = :i"
            ),
            {"i": mid_mock},
        ).fetchone()
        record(
            "R5 mock 活动指针已设置",
            mrow[0] is not None and mrow[1] is not None,
        )
        arow = conn.execute(
            text("SELECT storage_key, is_demo FROM audio_assets WHERE id = :i"),
            {"i": mrow[0]},
        ).fetchone()
        record(
            "R5 mock is_demo + 缺失音频 storage_key=NULL",
            arow[0] is None and arow[1] is True,
        )

        srows = conn.execute(
            text(
                "SELECT seq_index, start_ms, end_ms, source_transcript_id "
                "FROM transcript_segments WHERE version_id = :v ORDER BY seq_index"
            ),
            {"v": mrow[1]},
        ).fetchall()
        record("R4 去重后片段数=2", len(srows) == 2, f"got {len(srows)}")
        if srows:
            record(
                "R4 负时间置空 + 正常秒转毫秒",
                srows[0][1] == 0 and srows[0][2] == 1000 and srows[1][1] is None,
                str([(s[0], s[1], s[2]) for s in srows]),
            )
            record(
                "R5 旧片段 ID 映射保留",
                all(s[3] is not None for s in srows),
            )

        urow = conn.execute(
            text(
                "SELECT active_audio_id, active_transcript_version_id "
                "FROM meetings WHERE id = :i"
            ),
            {"i": mid_unknown},
        ).fetchone()
        record("R5 未知来源隔离(不设活动指针)", urow[0] is None and urow[1] is None)

        ivrow = conn.execute(
            text(
                "SELECT active_transcript_version_id FROM meetings WHERE id = :i"
            ),
            {"i": mid_inv},
        ).fetchone()
        iseg = conn.execute(
            text(
                "SELECT start_ms, end_ms FROM transcript_segments WHERE version_id = :v"
            ),
            {"v": ivrow[0]},
        ).fetchone()
        record(
            "F4 倒置时间成对清空",
            iseg is not None and iseg[0] is None and iseg[1] is None,
            f"got {iseg}",
        )

        # 清理：删除种子课时（级联删除其转写/音频/版本/片段）
        conn.execute(
            text("DELETE FROM meetings WHERE id IN (:a, :b, :c)"),
            {"a": mid_mock, "b": mid_unknown, "c": mid_inv},
        )

    async with engine.connect() as conn:
        await conn.run_sync(_sync)


async def phase_g4_g5() -> None:
    """G4 删除课时清理产物端证据关联 / G5 归档知识普通删除拒绝"""
    subject_a = ScopeContext(subject_id="local-user")

    async with async_session_factory() as db:
        # ── G4 删除课时不残留产物端证据关联 ───────────────────
        course = await course_service.create_course(
            db, subject_a, CourseCreate(title="G4证据清理", teacher="T", term="2026秋")
        )
        src = await course_service.create_lecture(
            db, subject_a, course.id, LectureCreate(title="来源课时")
        )
        tgt = await course_service.create_lecture(
            db, subject_a, course.id, LectureCreate(title="产物课时")
        )

        audio = AudioAsset(lecture_id=src.id, storage_key=None, is_demo=False)
        db.add(audio)
        await db.flush()
        version = TranscriptVersion(
            audio_asset_id=audio.id, lecture_id=src.id, version=1, status="active"
        )
        db.add(version)
        await db.flush()
        db.add(
            TranscriptSegment(
                version_id=version.id, seq_index=0, speaker="老师",
                content="证据来源", start_ms=0, end_ms=1000,
            )
        )
        await db.flush()
        span = EvidenceSpan(
            transcript_version_id=version.id, segment_start=0, segment_end=0,
            start_ms=0, end_ms=1000, quote="证据来源", is_valid=True,
        )
        db.add(span)
        await db.flush()

        # 跨课时：产物在 tgt，证据在 src
        cross_summary = Summary(meeting_id=tgt.id, content="跨课时纪要", status="generated")
        cross_decision = Decision(meeting_id=tgt.id, title="跨课时决策")
        db.add_all([cross_summary, cross_decision])
        await db.flush()
        await evidence_service.link_artifact(db, subject_a, "summary", cross_summary.id, span.id)
        await evidence_service.link_artifact(db, subject_a, "decision", cross_decision.id, span.id)

        # 同课时：产物在 src，证据也在 src
        same_summary = Summary(meeting_id=src.id, content="同课时纪要", status="generated")
        db.add(same_summary)
        await db.flush()
        await evidence_service.link_artifact(db, subject_a, "summary", same_summary.id, span.id)

        # 删除产物课时 tgt：跨课时产物端关联清理，来源证据保留
        await meeting_service.delete_meeting(db, tgt.id)
        cross_remaining = (
            await db.execute(
                select(func.count(ArtifactEvidence.id)).where(
                    ArtifactEvidence.artifact_id.in_([cross_summary.id, cross_decision.id])
                )
            )
        ).scalar_one()
        record(
            "G4 跨课时删除清理产物端证据关联",
            cross_remaining == 0,
            f"remaining={cross_remaining}",
        )
        span_remaining = (
            await db.execute(
                select(func.count(EvidenceSpan.id)).where(EvidenceSpan.id == span.id)
            )
        ).scalar_one()
        record("G4 跨课时删除保留来源证据", span_remaining == 1, f"remaining={span_remaining}")

        # 删除来源课时 src：同课时产物端关联清理
        await meeting_service.delete_meeting(db, src.id)
        same_remaining = (
            await db.execute(
                select(func.count(ArtifactEvidence.id)).where(
                    ArtifactEvidence.artifact_id == same_summary.id
                )
            )
        ).scalar_one()
        record(
            "G4 同课时删除清理产物端证据关联",
            same_remaining == 0,
            f"remaining={same_remaining}",
        )

        # ── G5 归档知识普通删除被拒绝 ──────────────────────────
        course2 = await course_service.create_course(
            db, subject_a, CourseCreate(title="G5归档知识", teacher="T", term="2026秋")
        )
        lecture2 = await course_service.create_lecture(
            db, subject_a, course2.id, LectureCreate(title="归档知识来源")
        )
        kdoc = KnowledgeDocument(
            title="归档知识文档",
            owner_id="local-user",
            source_type="meeting_summary",
            source_id=lecture2.id,
            content="归档知识内容",
            embedding=None,
        )
        db.add(kdoc)
        await db.flush()

        # 归档课程：其下课时来源的知识块不再可访问
        await course_service.delete_course(db, subject_a, course2.id)

        deleted = await knowledge_service.delete_document(db, kdoc.id, scope=subject_a)
        still = (
            await db.execute(
                select(func.count(KnowledgeDocument.id)).where(
                    KnowledgeDocument.id == kdoc.id
                )
            )
        ).scalar_one()
        record(
            "G5 归档知识普通删除被拒绝",
            deleted is False and still == 1,
            f"deleted={deleted} still={still}",
        )

    # 不 commit：会话关闭即回滚


async def run_async_phases() -> None:
    await phase_services()
    await phase_db_constraints()
    await phase_backfill()
    await phase_g4_g5()


def main() -> None:
    check_test_target()
    asyncio.run(run_async_phases())

    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        extra = f"  ({detail})" if detail and not ok else ""
        print(f"[{mark}] {name}{extra}")
    print("-" * 60)
    print(f"TOTAL {len(RESULTS)}  PASS {len(RESULTS) - len(failed)}  FAIL {len(failed)}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
