"""阶段一旧数据回填（共享实现）

fresh 迁移与补偿迁移复用同一份实现，保证两条升级路径结果一致。
用 Python 处理去重 / 时间校验 / 活动指针 / is_demo / 来源映射，
SQL 仅做批量写；全部写通过传入的 sync connection（迁移上下文）。

可信与演示分类（对应验收 R5）：

- ``transcription_mode == 'real'``   → 可信，is_demo=False，设活动指针
- ``transcription_mode == 'mock'``   → 演示，is_demo=True，设活动指针
- ``transcription_mode IS NULL``     → 未知来源，隔离（is_demo=True，不设活动指针）

缺失音频（audio_url 为空）时 storage_key 置 NULL 显式表示「无音频」，
不伪造空字符串 storage_key。

幂等：对「尚无任何 transcript_version 的课时」建立初始版本；
对「已有版本但字段与分类不一致」的旧回填记录做可确定字段修复（G2），
保留版本 / 证据稳定 ID，不重建、不删除；未知来源或存在冲突的记录隔离并报告。
"""

import math
import uuid

from sqlalchemy import text

HISTORY_COURSE_TITLE = "待整理历史资料"
HISTORY_COURSE_TERM = "历史导入"


def _sec_to_ms(value) -> int | None:
    """秒 → 毫秒；空值 / 负值 / 非有限值置空"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None
    return int(round(f * 1000))


def _parse_pair_ms(start, end) -> tuple[int | None, int | None, bool]:
    """秒对 → 毫秒对。返回 ``(start_ms, end_ms, anomaly)``。

    ``anomaly=True`` 表示存在负值 / 非有限值 / 倒置（start > end）等异常。
    - 单个值异常（负/非有限）：仅清空该值，保留另一合法端点。
    - 倒置（start > end）：成对清空，不静默交换、不伪造来源。
    正文始终保留。
    """
    s, e = _sec_to_ms(start), _sec_to_ms(end)
    anomaly = False
    if start is not None and s is None:
        anomaly = True
    if end is not None and e is None:
        anomaly = True
    if s is not None and e is not None and s > e:
        s, e = None, None
        anomaly = True
    return s, e, anomaly


def backfill_legacy_course(conn, owner_id: str) -> None:
    """旧会议归入「待整理历史资料」课程并标记 legacy_meeting（幂等）。"""
    row = conn.execute(
        text(
            "SELECT id FROM courses WHERE owner_id = :o AND term = :t "
            "ORDER BY created_at ASC LIMIT 1"
        ),
        {"o": owner_id, "t": HISTORY_COURSE_TERM},
    ).fetchone()
    if row is not None:
        course_id = row[0]
    else:
        course_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO courses (id, owner_id, title, term, created_at, updated_at) "
                "VALUES (:id, :o, :title, :term, now(), now())"
            ),
            {
                "id": course_id,
                "o": owner_id,
                "title": HISTORY_COURSE_TITLE,
                "term": HISTORY_COURSE_TERM,
            },
        )

    conn.execute(
        text(
            "UPDATE meetings SET course_id = :cid, legacy_meeting = TRUE "
            "WHERE course_id IS NULL"
        ),
        {"cid": course_id},
    )


def _is_backfill_candidate(conn, vid, provider, version_number, kept) -> bool:
    """判断既有版本是否为「可确定的旧回填候选」。

    回填创建的初始版本特征：provider 为空、version=1，且其片段正文与旧转写
    一致（可建立 source_transcript_id 映射）。正常产品（真实/降级处理）必有
    非空 provider、版本号递增、正文可能不同，不应被旧回填修复改写（对应 H1）。
    """
    if provider is not None:
        return False
    if version_number != 1:
        return False
    old_content_by_seq = {r[1]: r[3] for r in kept}
    segs = conn.execute(
        text("SELECT seq_index, content FROM transcript_segments WHERE version_id = :v"),
        {"v": vid},
    ).fetchall()
    if not segs:
        return False
    for seq, content in segs:
        if old_content_by_seq.get(seq) != content:
            return False
    return True


def _repair_existing_versions(
    conn, report, mid, versions, kept, is_demo, set_active, version_status
) -> None:
    """修复既有版本（G2 / H1 / H2），保留版本 / 证据稳定 ID。

    - 只把「可确定的旧回填候选」（provider 为空、version=1、正文与旧转写一致）
      视为可修复对象；其余正常产品保持原有状态、演示标记与指针（H1）。
    - 候选只校正自身字段：is_demo 与旧片段映射（不依赖唯一性）；
      状态与活动指针仅在「恰好一个候选且无其他版本」时可控。
    - 未知来源无论版本数量都撤销活动暴露（H2），不因多版本提前返回。
    """
    old_by_key: dict[tuple[int, str], object] = {
        (r[1], r[3]): r[0] for r in kept
    }

    candidates: list[tuple] = []
    non_candidates: list[tuple] = []
    for vid, aid, status, provider, vnum in versions:
        if _is_backfill_candidate(conn, vid, provider, vnum, kept):
            candidates.append((vid, aid, status, provider, vnum))
        else:
            non_candidates.append((vid, aid, status, provider, vnum))

    # 正常产品：保持原状，只记录处置（H1 要求不改写 failed/building/superseded 等）
    for vid, aid, status, provider, vnum in non_candidates:
        report["preserved_versions"].append(
            {
                "lecture_id": str(mid),
                "version_id": str(vid),
                "provider": provider,
                "version": vnum,
                "status": status,
                "reason": "not_legacy_backfill_candidate",
            }
        )

    # 候选：修复自身字段（is_demo + 旧片段映射），不依赖版本唯一性
    for vid, aid, status, provider, vnum in candidates:
        if aid is not None:
            arow = conn.execute(
                text("SELECT is_demo FROM audio_assets WHERE id = :a"), {"a": aid}
            ).fetchone()
            if arow is not None and bool(arow[0]) != is_demo:
                conn.execute(
                    text("UPDATE audio_assets SET is_demo = :d WHERE id = :a"),
                    {"d": is_demo, "a": aid},
                )
                report["repaired_is_demo"] += 1

        segs = conn.execute(
            text(
                "SELECT id, seq_index, content FROM transcript_segments "
                "WHERE version_id = :v AND source_transcript_id IS NULL"
            ),
            {"v": vid},
        ).fetchall()
        for sid, seq, content in segs:
            src = old_by_key.get((seq, content))
            if src is not None:
                conn.execute(
                    text(
                        "UPDATE transcript_segments SET source_transcript_id = :s "
                        "WHERE id = :i"
                    ),
                    {"s": src, "i": sid},
                )
                report["repaired_source_mapping"] += 1

    # 未知来源：无论版本数量都撤销活动暴露（H2），保留版本 / 证据 ID
    if not set_active:
        report["unknown_source_isolated"] += 1
        prow = conn.execute(
            text(
                "SELECT active_audio_id, active_transcript_version_id "
                "FROM meetings WHERE id = :m"
            ),
            {"m": mid},
        ).fetchone()
        cur_a = prow[0] if prow else None
        cur_v = prow[1] if prow else None
        if cur_a is not None or cur_v is not None:
            conn.execute(
                text(
                    "UPDATE meetings SET active_audio_id = NULL, "
                    "active_transcript_version_id = NULL WHERE id = :m"
                ),
                {"m": mid},
            )
            report["repaired_active_pointer"] += 1

        # 候选版本状态校正为隔离态（ready）；非候选保持原状
        for vid, aid, status, provider, vnum in candidates:
            if status != version_status:
                conn.execute(
                    text("UPDATE transcript_versions SET status = :s WHERE id = :v"),
                    {"s": version_status, "v": vid},
                )
                report["repaired_version_status"] += 1

        for vid, aid, status, provider, vnum in versions:
            report["isolated_versions"].append(
                {
                    "lecture_id": str(mid),
                    "version_id": str(vid),
                    "provider": provider,
                    "version": vnum,
                    "reason": "unknown_source_isolated",
                }
            )
        return

    # 可信 / 演示来源：状态 + 活动指针仅在「恰好一个候选且无其他版本」时可控
    if len(candidates) == 1 and len(non_candidates) == 0:
        vid, aid, status, provider, vnum = candidates[0]
        if status != version_status:
            conn.execute(
                text("UPDATE transcript_versions SET status = :s WHERE id = :v"),
                {"s": version_status, "v": vid},
            )
            report["repaired_version_status"] += 1

        prow = conn.execute(
            text(
                "SELECT active_audio_id, active_transcript_version_id "
                "FROM meetings WHERE id = :m"
            ),
            {"m": mid},
        ).fetchone()
        cur_a = prow[0] if prow else None
        cur_v = prow[1] if prow else None
        if cur_a != aid or cur_v != vid:
            conn.execute(
                text(
                    "UPDATE meetings SET active_audio_id = :a, "
                    "active_transcript_version_id = :v WHERE id = :m"
                ),
                {"a": aid, "v": vid, "m": mid},
            )
            report["repaired_active_pointer"] += 1
    elif len(candidates) > 1 or (candidates and non_candidates):
        # 多个候选，或候选与正常产品并存：冲突/歧义，不发布、不清空
        # （可信来源的合法历史版本不能被统一清空破坏，见 H2 修复要求）
        report["existing_versions_ambiguous"] += 1
        for vid, aid, status, provider, vnum in versions:
            report["ambiguous_versions"].append(
                {
                    "lecture_id": str(mid),
                    "version_id": str(vid),
                    "provider": provider,
                    "version": vnum,
                    "status": status,
                    "reason": "ambiguous_active_version",
                }
            )
    # else: 无候选（全部为正常产品）——保持原状，无需处理


def backfill_transcript_versions(conn, owner_id: str) -> dict:
    """给旧转写建立初始版本，返回可追溯的异常 / 去重报告。

    去重策略：同一会议内按 (seq_index, created_at, id) 确定性排序，
    保留最早一条，其余丢弃并计数，逐条写入 ``dropped_details``（含保留原因）。
    旧片段 ID 通过 transcript_segments.source_transcript_id 保留映射。

    幂等：仅处理尚无 transcript_version 的课时，避免重复调用产生多份版本。
    """
    report = {
        "meetings_with_transcripts": 0,
        "audio_assets_created": 0,
        "versions_created": 0,
        "segments_created": 0,
        "segments_dropped_duplicate_seq": 0,
        "segments_times_invalid": 0,
        "missing_audio_meetings": 0,
        "unknown_source_isolated": 0,
        "dropped_details": [],
        # 修复路径（G2）：已有版本但字段与分类不一致时的可确定字段修复计数
        "repaired_is_demo": 0,
        "repaired_version_status": 0,
        "repaired_active_pointer": 0,
        "repaired_source_mapping": 0,
        "existing_versions_ambiguous": 0,
        # 处置明细（H1/H2 要求可追踪课时/版本身份与原因）
        "preserved_versions": [],
        "isolated_versions": [],
        "ambiguous_versions": [],
    }

    meeting_rows = conn.execute(
        text("SELECT DISTINCT t.meeting_id FROM transcripts t ORDER BY t.meeting_id")
    ).fetchall()
    meeting_ids = [r[0] for r in meeting_rows]
    report["meetings_with_transcripts"] = len(meeting_ids)

    for mid in meeting_ids:
        mrow = conn.execute(
            text("SELECT transcription_mode, audio_url FROM meetings WHERE id = :m"),
            {"m": mid},
        ).fetchone()
        transcription_mode = mrow[0] if mrow else None
        audio_url = mrow[1] if mrow else None

        # 可信 / 演示 / 未知 分类
        is_demo = transcription_mode != "real"
        set_active = transcription_mode in ("real", "mock")
        version_status = "active" if set_active else "ready"

        # 读取旧片段并按 (seq_index, created_at, id) 确定性去重
        old_rows = conn.execute(
            text(
                "SELECT id, seq_index, speaker, content, start_time, end_time "
                "FROM transcripts WHERE meeting_id = :m "
                "ORDER BY seq_index ASC, created_at ASC, id ASC"
            ),
            {"m": mid},
        ).fetchall()
        seen_seq: set[int] = set()
        kept: list[tuple] = []
        for r in old_rows:
            seq = r[1]
            if seq in seen_seq:
                report["segments_dropped_duplicate_seq"] += 1
                report["dropped_details"].append(
                    {
                        "transcript_id": str(r[0]),
                        "seq_index": seq,
                        "reason": "duplicate_seq",
                    }
                )
                continue
            seen_seq.add(seq)
            kept.append(r)

        if not kept:
            continue

        # 已有版本 → 修复路径（G2），不再无条件跳过
        existing = conn.execute(
            text(
                "SELECT id, audio_asset_id, status, provider, version "
                "FROM transcript_versions "
                "WHERE lecture_id = :m ORDER BY created_at ASC, id ASC"
            ),
            {"m": mid},
        ).fetchall()
        if existing:
            _repair_existing_versions(
                conn, report, mid, existing, kept, is_demo, set_active, version_status
            )
            continue

        # ── 无既有版本 → 建立初始版本 ─────────────────────────
        if not set_active:
            report["unknown_source_isolated"] += 1

        # 音频资产：缺失音频用 NULL storage_key 显式表示
        if not audio_url:
            report["missing_audio_meetings"] += 1
        audio_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO audio_assets "
                "(id, lecture_id, storage_key, version, is_demo, created_at) "
                "VALUES (:id, :lid, :key, 1, :demo, now())"
            ),
            {"id": audio_id, "lid": mid, "key": audio_url or None, "demo": is_demo},
        )
        report["audio_assets_created"] += 1

        # 转写版本
        version_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO transcript_versions "
                "(id, audio_asset_id, lecture_id, version, provider, status, created_at) "
                "VALUES (:id, :aid, :lid, 1, NULL, :status, now())"
            ),
            {"id": version_id, "aid": audio_id, "lid": mid, "status": version_status},
        )
        report["versions_created"] += 1

        # 片段：秒对 → 毫秒对，负值/非有限/倒置清空；保留旧片段 ID 映射
        for r in kept:
            old_id, seq, speaker, content, start_time, end_time = r
            sms, ems, anomaly = _parse_pair_ms(start_time, end_time)
            if anomaly:
                report["segments_times_invalid"] += 1
            conn.execute(
                text(
                    "INSERT INTO transcript_segments "
                    "(id, version_id, seq_index, speaker, content, start_ms, end_ms, "
                    " source_transcript_id, created_at) "
                    "VALUES (:id, :vid, :seq, :spk, :content, :sms, :ems, :src, now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "vid": version_id,
                    "seq": seq,
                    "spk": speaker,
                    "content": content,
                    "sms": sms,
                    "ems": ems,
                    "src": old_id,
                },
            )
            report["segments_created"] += 1

        # 活动指针：仅可信 / 演示来源
        if set_active:
            conn.execute(
                text(
                    "UPDATE meetings SET active_audio_id = :aid, "
                    "active_transcript_version_id = :vid WHERE id = :mid"
                ),
                {"aid": audio_id, "vid": version_id, "mid": mid},
            )

    return report
