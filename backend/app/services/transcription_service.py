"""音频转写服务（Provider 抽象 + 自动降级）

转写链路（按 TRANSCRIPTION_PROVIDER 配置）：
- auto（默认）：DashScope 原生 ASR + OSS → 失败降级 Mock
- dashscope：仅 DashScope 原生 ASR（失败抛错，不降级）
- mock：仅 Mock（本地演示用）

真实转写流程（DashScope 原生 ASR）：
1. 本地文件 → 上传 OSS → 获取公网 URL
2. 提交 DashScope 录音识别任务（异步）
3. 轮询任务状态直到 SUCCEEDED
4. 解析带时间戳和说话人的转写片段
5. 清理 OSS 上的临时文件

Mock 降级场景：
- 未配置 OSS 或 DashScope API Key
- 未安装 oss2 依赖
- DashScope API 调用失败（网络、额度、超时）
- TRANSCRIPTION_PROVIDER=mock
"""

import logging
import os
from typing import Optional

from app.config import settings
from app.services.dashscope_asr_service import dashscope_asr_service
from app.services.oss_service import oss_service

logger = logging.getLogger(__name__)


# ============================================================
# Mock 转写语料（模拟一场技术面试的常见对话）
# ============================================================
_MOCK_SEGMENTS = [
    ("面试官", "你好，欢迎参加今天的技术面试，我们先做个简单的自我介绍吧。"),
    ("候选人", "好的，我是一名有五年经验的后端工程师，主要做 Python 和 Go。"),
    ("面试官", "能讲一下你最近做的项目吗？架构上有什么挑战？"),
    ("候选人", "最近做了一个微服务架构的会议系统，主要挑战在实时音视频同步。"),
    ("面试官", "微服务拆分时你是怎么界定边界的？"),
    ("候选人", "按业务领域拆分，每个服务独立数据库，通过 gRPC 通信。"),
    ("面试官", "遇到过分布式事务的问题吗？怎么解决的？"),
    ("候选人", "用的是 Saga 模式，每个本地事务有补偿操作，最终一致性。"),
    ("面试官", "数据库选型上为什么用 PostgreSQL？"),
    ("候选人", "主要是 pgvector 扩展支持向量检索，做 RAG 知识库比较方便。"),
    ("面试官", "向量索引用的什么算法？HNSW 还是 IVF？"),
    ("候选人", "HNSW，召回率高，查询延迟稳定在 50ms 以内。"),
    ("面试官", "前端这块你怎么处理实时转写展示的？"),
    ("候选人", "用 WebSocket 推送，前端虚拟滚动避免长列表卡顿。"),
    ("面试官", "如果转录服务挂了怎么办？有降级方案吗？"),
    ("候选人", "有，本地降级 Mock，让用户至少能看到流程跑通。"),
    ("面试官", "好的，技术上没问题。你有什么问题想问我的？"),
    ("候选人", "想了解下团队的工程规范和 Code Review 流程。"),
    ("面试官", "我们用 GitLab MR + 双人 Review，CI 跑测试和 lint 才能合并。"),
    ("候选人", "明白了，谢谢，我对这个岗位很感兴趣。"),
    ("面试官", "好的，今天的面试就到这里，HR 会后续联系你。"),
    ("候选人", "好的，再见。"),
]


class TranscriptionService:
    """音频转写服务

    自动在 DashScope 真实转写与 Mock 之间切换，返回 (segments, mode)。
    mode: 'real' / 'mock'
    """

    def __init__(self):
        self.provider = settings.TRANSCRIPTION_PROVIDER

    async def transcribe(
        self, audio_path: str, language: str = "zh"
    ) -> tuple[Optional[list[dict]], str]:
        """
        转写音频文件

        Returns:
            (segments, mode)
            - segments: 转写片段列表；None 表示彻底无法转写
            - mode: 'real'(真实API) / 'mock'(本地降级)
        """
        # 前置检查：音频路径必须存在
        if not audio_path or not os.path.exists(audio_path):
            logger.error(f"音频文件不存在: {audio_path!r}")
            return None, "mock"

        provider = self.provider

        # 强制 Mock 模式
        if provider == "mock":
            logger.info("TRANSCRIPTION_PROVIDER=mock，使用 Mock 转写")
            return await self._mock_transcribe(audio_path), "mock"

        # auto / dashscope：尝试真实转写
        if provider in ("auto", "dashscope"):
            segments = await self._transcribe_via_dashscope(audio_path, language)
            if segments is not None:
                return segments, "real"

            # dashscope 模式不降级
            if provider == "dashscope":
                logger.error("dashscope 模式转写失败，不降级")
                return None, "real"

            # auto 模式降级到 Mock
            logger.warning("真实转写失败，降级到 Mock")
            return await self._mock_transcribe(audio_path), "mock"

        # 未知 provider
        logger.warning(f"未知 TRANSCRIPTION_PROVIDER={provider}，使用 Mock")
        return await self._mock_transcribe(audio_path), "mock"

    async def _transcribe_via_dashscope(
        self, audio_path: str, language: str
    ) -> Optional[list[dict]]:
        """
        通过 DashScope 原生 ASR + OSS 真实转写

        流程：本地文件 → OSS → 公网 URL → DashScope ASR → 解析结果 → 清理 OSS
        """
        # 1. 检查前置条件
        if not dashscope_asr_service.is_available:
            logger.warning("DashScope API Key 未配置，跳过真实转写")
            return None

        if not oss_service.is_available:
            logger.warning("OSS 未配置或 oss2 未安装，跳过真实转写")
            return None

        # 2. 上传到 OSS 获取公网 URL
        audio_url = oss_service.upload_audio(audio_path)
        if not audio_url:
            logger.error("上传 OSS 失败，跳过真实转写")
            return None

        # 3. 调用 DashScope ASR
        try:
            language_hints = [language] if language else ["zh", "en"]
            segments = await dashscope_asr_service.transcribe(
                audio_url, language_hints=language_hints
            )
            if segments:
                logger.info(f"DashScope 真实转写成功，共 {len(segments)} 个片段")
                return segments
            logger.error("DashScope ASR 返回空结果")
            return None
        except Exception as e:
            logger.error(f"DashScope ASR 调用异常: {e}")
            return None
        finally:
            # 4. 清理 OSS 临时文件（无论成功失败）
            oss_service.delete_object(audio_url)

    async def _mock_transcribe(self, audio_path: str) -> list[dict]:
        """
        生成 Mock 转写结果
        - 根据音频文件大小估算时长，裁剪/循环语料到匹配长度
        - 每个片段分配合理的时间戳
        """
        # 估算音频时长：mp3 ~ 1MB/分钟（128kbps）
        try:
            file_size = os.path.getsize(audio_path)
        except OSError:
            file_size = 5 * 1024 * 1024  # 默认按 5 分钟算

        estimated_minutes = max(1, file_size / (1024 * 1024))
        estimated_duration = estimated_minutes * 60.0  # 秒

        # 根据时长选择片段数量（每段约 15-25 秒）
        target_segments = max(4, min(len(_MOCK_SEGMENTS), int(estimated_duration / 20)))

        # 选择片段（前 N 条，保证内容稳定可预测）
        chosen = list(_MOCK_SEGMENTS[:target_segments])

        # 时间戳分配
        segments = []
        seg_duration = estimated_duration / len(chosen)
        for i, (speaker, content) in enumerate(chosen):
            start = i * seg_duration
            end = start + seg_duration - 0.5
            segments.append(
                {
                    "speaker": speaker,
                    "content": content,
                    "start_time": round(start, 2),
                    "end_time": round(max(start, end), 2),
                    "seq_index": i,
                }
            )
        logger.info(
            f"Mock 转写完成：{len(segments)} 个片段，估算时长 {estimated_minutes:.1f} 分钟"
        )
        return segments

    async def transcribe_and_store(
        self, meeting_id: str, audio_path: str
    ) -> tuple[bool, str]:
        """
        转写音频并存储到数据库

        Returns:
            (success, mode)
            - success: True 表示转写并存储成功
            - mode: 'real' / 'mock'
        """
        from app.db.session import async_session_factory
        from app.models.transcript import Transcript
        from app.models.meeting import Meeting
        from sqlalchemy import update

        # 更新状态为转写中
        async with async_session_factory() as session:
            await session.execute(
                update(Meeting)
                .where(Meeting.id == meeting_id)
                .values(status="transcribing")
            )
            await session.commit()

        try:
            segments, mode = await self.transcribe(audio_path)
            if segments is None:
                return False, mode

            # 存储转写结果
            async with async_session_factory() as session:
                for seg in segments:
                    transcript = Transcript(
                        meeting_id=meeting_id,
                        speaker=seg["speaker"],
                        content=seg["content"],
                        start_time=seg["start_time"],
                        end_time=seg["end_time"],
                        seq_index=seg["seq_index"],
                    )
                    session.add(transcript)

                # 更新会议状态 + 记录转写模式
                await session.execute(
                    update(Meeting)
                    .where(Meeting.id == meeting_id)
                    .values(
                        status="processed",
                        transcription_mode=mode,
                    )
                )
                await session.commit()

            mode_label = "Mock" if mode == "mock" else "真实"
            logger.info(f"会议 {meeting_id} 转写结果已存储（{mode_label}转写）")
            return True, mode

        except Exception as e:
            logger.error(f"存储转写结果失败: {e}")
            async with async_session_factory() as session:
                await session.execute(
                    update(Meeting)
                    .where(Meeting.id == meeting_id)
                    .values(status="failed")
                )
                await session.commit()
            raise


# 全局实例
transcription_service = TranscriptionService()
