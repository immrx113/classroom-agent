"""DashScope 原生录音文件识别 API 客户端

调用流程（异步任务模式）：
1. POST 提交转写任务，返回 task_id
2. GET 轮询任务状态，直到 SUCCEEDED / FAILED
3. 从结果中提取带时间戳的转写片段

文档：
https://help.aliyun.com/zh/dashscope/developer-reference/paraformer-audio-file-recognition

特点：
- 中文识别率最优（paraformer-v2）
- 支持标点恢复、时间戳、说话人分离
- 需要公网可访问的音频 URL（通过 OSS 中转）
"""

import asyncio
import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class DashScopeASRHttpError(Exception):
    """单次查询返回非 2xx：保留 status 与 Retry-After，供上层分类/退避（CA-15/CA-20）。"""

    def __init__(self, status_code: int, retry_after: str | None = None):
        super().__init__(f"DashScope ASR HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


class DashScopeASRConnectionError(Exception):
    """提交/查询响应丢失（超时/连接中断/回执缺失）：无法证明供应商是否执行，须按 unknown 处理（CA-07/CA-20）。"""


class DashScopeASRService:
    """DashScope 原生录音识别服务"""

    def __init__(self):
        # API Key 优先用 DASHSCOPE_API_KEY，否则复用 OPENAI_API_KEY
        self.api_key = settings.DASHSCOPE_API_KEY or settings.OPENAI_API_KEY
        self.submit_url = settings.DASHSCOPE_ASR_URL
        self.query_url = settings.DASHSCOPE_TASK_QUERY_URL
        self.model = settings.DASHSCOPE_ASR_MODEL

    @property
    def is_available(self) -> bool:
        """是否配置了 API Key"""
        return bool(self.api_key)

    async def transcribe(
        self,
        audio_url: str,
        language_hints: Optional[list[str]] = None,
        timeout: int = 600,
        poll_interval: int = 5,
    ) -> Optional[list[dict]]:
        """
        提交录音识别任务并轮询结果

        Args:
            audio_url: 音频的公网 URL（OSS 地址）
            language_hints: 语言提示，如 ["zh", "en"]
            timeout: 最大等待秒数（默认 10 分钟）
            poll_interval: 轮询间隔秒数

        Returns:
            转写片段列表，每项包含 speaker, content, start_time, end_time, seq_index
            失败返回 None
        """
        if not self.is_available:
            logger.warning("DashScope API Key 未配置，无法真实转写")
            return None

        # 1. 提交任务
        task_id = await self._submit_task(audio_url, language_hints)
        if not task_id:
            return None

        # 2. 轮询结果
        result = await self._poll_task(task_id, timeout, poll_interval)
        if result is None:
            return None

        # 3. 解析结果
        return self._parse_result(result)

    async def _submit_task(
        self,
        audio_url: str,
        language_hints: Optional[list[str]] = None,
    ) -> Optional[str]:
        """提交录音识别任务

        关键：必须加 X-DashScope-Async: enable 头，否则 API 拒绝同步调用。
        文档：https://help.aliyun.com/zh/model-studio/paraformer-recorded-speech-recognition-restful-api
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # 关键：异步任务标识，缺失会被拒绝（403 AccessDenied）
            "X-DashScope-Async": "enable",
        }

        # 请求体结构参考官方文档
        body = {
            "model": self.model,
            "input": {
                "file_urls": [audio_url],
            },
            "parameters": {
                "language_hints": language_hints or ["zh", "en"],
                "disfluency_removal_enabled": True,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(self.submit_url, headers=headers, json=body)

            if resp.status_code != 200:
                # 保留 status 与 Retry-After，供上层分类/退避（CA-20：429 遵守提示、401/403 永久停止）
                raise DashScopeASRHttpError(resp.status_code, resp.headers.get("Retry-After"))

            data = resp.json()
            task_id = data.get("output", {}).get("task_id") if isinstance(data, dict) else None
            if not task_id:
                # 200 但无 task_id：供应商已受理但回执缺失，无法确认执行，按响应丢失处理（CA-07）
                raise DashScopeASRConnectionError("DashScope ASR 回执缺少 task_id")

            logger.info(f"DashScope ASR 任务已提交: task_id={task_id}")
            return task_id

        except (DashScopeASRHttpError, DashScopeASRConnectionError):
            raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            # 提交响应丢失（超时/连接中断）：无法证明供应商是否执行，交由上层按 unknown 阻断普通重试
            raise DashScopeASRConnectionError(str(e)) from e
        except Exception as e:
            # 其余未知异常（如回执解析失败）同属响应丢失，按 unknown 处理
            raise DashScopeASRConnectionError(str(e)) from e

    async def query_task_once(self, task_id: str) -> tuple[str, dict]:
        """单次查询任务状态并返回 (task_status, output)，不再内部循环轮询（CA-15）。

        非 2xx 抛 ``DashScopeASRHttpError``（保留 status/Retry-After），由网关分类退避。
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.query_url}/{task_id}"
        async with httpx.AsyncClient(timeout=settings.CHECKPOINT_EXT_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise DashScopeASRHttpError(resp.status_code, resp.headers.get("Retry-After"))
        data = resp.json()
        return data.get("output", {}).get("task_status"), data.get("output", {})

    async def _poll_task(
        self, task_id: str, timeout: int, poll_interval: int
    ) -> Optional[dict]:
        """轮询任务状态直到完成"""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.query_url}/{task_id}"

        elapsed = 0
        async with httpx.AsyncClient(timeout=30) as client:
            while elapsed < timeout:
                try:
                    resp = await client.get(url, headers=headers)
                    data = resp.json()
                except Exception as e:
                    logger.warning(f"轮询失败（将重试）: {e}")
                    await asyncio.sleep(poll_interval)
                    elapsed += poll_interval
                    continue

                task_status = data.get("output", {}).get("task_status")
                logger.debug(
                    f"ASR 任务 {task_id} 状态: {task_status} (elapsed={elapsed}s)"
                )

                if task_status == "SUCCEEDED":
                    logger.info(f"DashScope ASR 任务完成: {task_id}")
                    return data.get("output", {})

                if task_status == "FAILED":
                    error = data.get("output", {}).get("message", "未知错误")
                    logger.error(f"DashScope ASR 任务失败: {error}")
                    return None

                # PENDING / RUNNING 继续轮询
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

        logger.error(f"DashScope ASR 任务超时（{timeout}s）: {task_id}")
        return None

    def _parse_result(self, task_output: dict) -> Optional[list[dict]]:
        """
        解析 DashScope ASR 结果

        结果结构（SUCCEEDED 时）：
        {
            "task_id": "...",
            "task_status": "SUCCEEDED",
            "results": [
                {
                    "transcription_url": "https://...",  # 包含详细结果的 JSON 文件
                    "subtask_status": "SUCCEEDED"
                }
            ]
        }

        transcription_url 指向的 JSON 结构：
        {
            "file_url": "...",
            "properties": {...},
            "transcripts": [
                {
                    "channel_id": [0],
                    "content_duration_in_milliseconds": ...,
                    "text": "完整文本",
                    "sentences": [
                        {
                            "begin_time": 410,
                            "end_time": 3680,
                            "text": "句子内容",
                            "speaker_id": 0  # 说话人（若开启）
                        }
                    ]
                }
            ]
        }
        """
        try:
            results = task_output.get("results", [])
            if not results:
                logger.error("DashScope ASR 结果为空")
                return None

            # 通常只有一个文件结果
            first_result = results[0]
            if first_result.get("subtask_status") != "SUCCEEDED":
                logger.error(
                    f"子任务失败: {first_result.get('subtask_status')}"
                )
                return None

            # 详细结果可能在 inline 或 transcription_url
            # 优先用 inline（若 API 返回），否则需要下载 transcription_url
            transcripts = first_result.get("transcripts")

            # 部分版本 API 直接返回 transcripts
            if transcripts:
                return self._parse_transcripts(transcripts)

            # 否则需要下载 transcription_url
            transcription_url = first_result.get("transcription_url")
            if transcription_url:
                return self._fetch_and_parse(transcription_url)

            logger.error("DashScope ASR 结果缺少 transcripts 和 transcription_url")
            return None

        except Exception as e:
            logger.error(f"解析 ASR 结果失败: {e}")
            return None

    def _parse_transcripts(self, transcripts: list[dict]) -> list[dict]:
        """解析 transcripts 列表为统一格式"""
        segments = []
        seq = 0
        for transcript in transcripts:
            sentences = transcript.get("sentences", [])
            for sentence in sentences:
                speaker_id = sentence.get("speaker_id")
                segments.append(
                    {
                        "speaker": (
                            f"说话人{speaker_id + 1}"
                            if speaker_id is not None
                            else f"说话人{seq + 1}"
                        ),
                        "content": sentence.get("text", "").strip(),
                        "start_time": sentence.get("begin_time", 0) / 1000.0,  # ms → s
                        "end_time": sentence.get("end_time", 0) / 1000.0,
                        "seq_index": seq,
                    }
                )
                seq += 1
        logger.info(f"解析 ASR 转写结果：共 {len(segments)} 个片段")
        return segments

    def _fetch_and_parse(self, url: str) -> Optional[list[dict]]:
        """下载 transcription_url 指向的详细结果 JSON 并解析（同步阻塞）"""
        try:
            # 用同步 httpx，避免在已有 event loop 的上下文中嵌套
            with httpx.Client(timeout=30) as client:
                resp = client.get(url)
                data = resp.json()

            transcripts = data.get("transcripts", [])
            return self._parse_transcripts(transcripts)
        except Exception as e:
            logger.error(f"下载 transcription_url 失败: {e}")
            return None


# 全局实例
dashscope_asr_service = DashScopeASRService()
