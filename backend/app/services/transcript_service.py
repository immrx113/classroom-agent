"""转写版本、片段、结果校验与条件发布

每次处理固定 audio_asset_id，创建新的 transcript_version；
片段按 (version_id, seq_index) 唯一。结果先完整校验再发布，
发布为条件切换：只有当前有效输入仍等于本任务的 audio_asset_id 时
才切换活动指针，旧输入晚完成不能覆盖新输入（P02-11）。
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audio_asset import AudioAsset
from app.models.meeting import Meeting
from app.models.transcript_version import TranscriptVersion, TranscriptSegment
from app.services.errors import ERR_INVALID_RESULT, ERR_NO_SPEECH, ServiceError

logger = logging.getLogger(__name__)


def _to_ms(value) -> int | None:
    """毫秒值统一为整数毫秒；缺失返回 None。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ServiceError(422, ERR_INVALID_RESULT, "片段时间非数值", retryable=False)
    if f < 0:
        raise ServiceError(422, ERR_INVALID_RESULT, "片段时间为负值", retryable=False)
    return int(round(f))


def _ms_from(seg: dict, key_ms: str, key_sec: str) -> int | None:
    """从片段提取毫秒：优先 ``start_ms``/``end_ms``（毫秒），
    否则 ``start_time``/``end_time``（秒，旧接入）。"""
    if key_ms in seg:
        return _to_ms(seg[key_ms])
    if key_sec in seg:
        return _to_ms(seg[key_sec] * 1000)
    return None


def validate_segments(segments: list[dict], duration_ms: int | None) -> list[dict]:
    """全量按序校验：顺序、时间范围、空片段。返回规范化片段。

    - 空结果 → 静音/无法识别（no_speech），与调用失败分型
    - 序号乱序/重复、时间倒置/越界、正文为空 → 非法结果（invalid_result）
    - 时间缺失不伪造精确定位，保留为 NULL
    """
    if not segments:
        raise ServiceError(422, ERR_NO_SPEECH, "未识别到语音内容（静音或无法识别）", retryable=False)

    normalized: list[dict] = []
    seen_seq: set[int] = set()
    for seg in segments:
        content = (seg.get("content") or "").strip()
        if not content:
            raise ServiceError(422, ERR_INVALID_RESULT, "转写片段正文为空", retryable=False)

        seq = seg.get("seq_index")
        if seq is None:
            seq = len(normalized)
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ServiceError(422, ERR_INVALID_RESULT, f"片段序号非法：{seq!r}", retryable=False)
        if seq in seen_seq:
            raise ServiceError(422, ERR_INVALID_RESULT, f"片段序号重复：{seq}", retryable=False)
        seen_seq.add(seq)

        start_ms = _ms_from(seg, "start_ms", "start_time")
        end_ms = _ms_from(seg, "end_ms", "end_time")

        if start_ms is not None and end_ms is not None and start_ms > end_ms:
            raise ServiceError(422, ERR_INVALID_RESULT, f"片段时间倒置：start>{end_ms}", retryable=False)
        if duration_ms is not None:
            if start_ms is not None and start_ms > duration_ms:
                raise ServiceError(422, ERR_INVALID_RESULT, "片段起点超出音频时长", retryable=False)
            if end_ms is not None and end_ms > duration_ms:
                raise ServiceError(422, ERR_INVALID_RESULT, "片段终点超出音频时长", retryable=False)

        normalized.append(
            {
                "seq_index": seq,
                "speaker": seg.get("speaker"),
                "content": content,
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )

    seqs = sorted(n["seq_index"] for n in normalized)
    if seqs != list(range(len(seqs))):
        raise ServiceError(422, ERR_INVALID_RESULT, "片段序号不连续或乱序", retryable=False)
    return normalized


async def create_version(
    db: AsyncSession,
    *,
    lecture_id: uuid.UUID,
    audio_asset_id: uuid.UUID,
    provider: str,
    provider_task_id: str | None,
    segments: list[dict],
) -> tuple[TranscriptVersion, bool]:
    """校验并保存转写版本，条件发布；返回 (version, is_active)。"""
    asset = await db.get(AudioAsset, audio_asset_id)
    duration_ms = asset.duration_ms if asset is not None else None
    normalized = validate_segments(segments, duration_ms)

    version = TranscriptVersion(
        audio_asset_id=audio_asset_id,
        lecture_id=lecture_id,
        version=1,
        provider=provider,
        provider_task_id=provider_task_id,
        status="building",
    )
    db.add(version)
    await db.flush()

    for seg in normalized:
        db.add(
            TranscriptSegment(
                version_id=version.id,
                seq_index=seg["seq_index"],
                speaker=seg["speaker"],
                content=seg["content"],
                start_ms=seg["start_ms"],
                end_ms=seg["end_ms"],
            )
        )
    await db.flush()

    is_active = await _publish_if_current(
        db, version, lecture_id, audio_asset_id
    )
    await db.flush()
    return version, is_active


async def _publish_if_current(
    db: AsyncSession,
    version: TranscriptVersion,
    lecture_id: uuid.UUID,
    audio_asset_id: uuid.UUID,
) -> bool:
    """条件发布：仅当课时当前有效输入仍为本任务音频时才切换指针。"""
    meeting = await db.get(Meeting, lecture_id)
    if meeting is None:
        raise ServiceError(404, "invalid_input", "课时不存在", retryable=False)

    if meeting.active_audio_id is not None and meeting.active_audio_id != audio_asset_id:
        # 旧输入晚完成：不覆盖新输入指针，版本保留为 ready（完整但非当前）
        version.status = "ready"
        return False

    # 撤销旧有效版本
    if meeting.active_transcript_version_id is not None and meeting.active_transcript_version_id != version.id:
        old = await db.get(TranscriptVersion, meeting.active_transcript_version_id)
        if old is not None:
            old.status = "superseded"

    meeting.active_audio_id = audio_asset_id
    meeting.active_transcript_version_id = version.id
    meeting.status = "processed"
    version.status = "active"
    return True
