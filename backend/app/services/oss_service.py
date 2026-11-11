"""阿里云 OSS 文件上传服务

用途：DashScope 原生录音识别 API 需要公网可访问的 URL，
本地音频文件需先上传到 OSS 获取公网 URL 再提交转写。

依赖：oss2（可选）
    pip install oss2

未安装 oss2 或未配置 OSS 时，is_available 返回 False，
TranscriptionService 会自动降级到 Mock。
"""

import logging
import os
import uuid
from urllib.parse import urlsplit, unquote
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class OSSService:
    """阿里云 OSS 文件上传服务"""

    def __init__(self):
        self._enabled = False
        self._bucket = None

        # 配置检查
        if not all([
            settings.OSS_ACCESS_KEY_ID,
            settings.OSS_ACCESS_KEY_SECRET,
            settings.OSS_ENDPOINT,
            settings.OSS_BUCKET_NAME,
        ]):
            logger.info("OSS 未配置，课堂真实转写不可用")
            return

        # 依赖检查（延迟导入）
        try:
            import oss2  # type: ignore
        except ImportError:
            logger.warning(
                "未安装 oss2 依赖，真实转写将降级到 Mock。"
                "如需启用：pip install oss2"
            )
            return

        try:
            auth = oss2.Auth(
                settings.OSS_ACCESS_KEY_ID,
                settings.OSS_ACCESS_KEY_SECRET,
            )
            self._bucket = oss2.Bucket(
                auth, settings.OSS_ENDPOINT, settings.OSS_BUCKET_NAME
            )
            self._enabled = True
            logger.info(f"OSS 服务已初始化: {settings.OSS_BUCKET_NAME}")
        except Exception as e:
            logger.error(f"OSS 初始化失败: {e}")

    @property
    def is_available(self) -> bool:
        """OSS 是否可用"""
        return self._enabled

    def upload_audio_object(self, local_path: str) -> Optional[str]:
        """上传音频到 OSS，返回永久 object_key（失败返回 None）。"""
        if not self.is_available:
            return None
        try:
            ext = os.path.splitext(local_path)[1] or ".mp3"
            object_key = f"{settings.OSS_PREFIX}{uuid.uuid4().hex}{ext}"
            self._bucket.put_object_from_file(object_key, local_path)
            logger.info("音频已上传到OSS: %s", object_key)
            return object_key
        except Exception as e:
            logger.error(f"OSS 上传失败: {e}")
            return None

    def sign_url(self, object_key: str) -> Optional[str]:
        """为 object_key 生成短时签名 URL（私有 Bucket 不记录签名）。"""
        if not self.is_available or not object_key:
            return None
        try:
            return self._bucket.sign_url("GET", object_key, 3600)
        except Exception as e:
            logger.error(f"OSS 签名失败: {e}")
            return None

    def upload_audio(self, local_path: str) -> Optional[str]:
        """
        上传音频文件到 OSS，返回公网可访问的 URL

        Args:
            local_path: 本地音频文件路径

        Returns:
            公网 URL，失败返回 None
        """
        object_key = self.upload_audio_object(local_path)
        if object_key is None:
            return None
        url = self.sign_url(object_key)
        if url is None:
            self.delete_object_key(object_key)
            return None
        return url

    def delete_object_key(self, object_key: str) -> bool:
        """按永久 object_key 删除对象（终态后清理，可重试）。"""
        if not self.is_available or not object_key:
            return False
        try:
            self._bucket.delete_object(object_key)
            logger.info(f"OSS 对象已删除: {object_key}")
            return True
        except Exception as e:
            logger.warning(f"OSS 删除失败: {e}")
            return False

    def delete_object(self, url: str) -> bool:
        """删除 OSS 上的对象（转写完成后清理）"""
        if not self.is_available or not url:
            return False

        try:
            # 从 URL 提取 object_key
            endpoint_host = settings.OSS_ENDPOINT.replace("https://", "").replace("http://", "")
            prefix = f"https://{settings.OSS_BUCKET_NAME}.{endpoint_host}/"
            if url.startswith(prefix):
                object_key = unquote(urlsplit(url).path.lstrip("/"))
                self._bucket.delete_object(object_key)
                logger.info(f"OSS 对象已删除: {object_key}")
                return True
        except Exception as e:
            logger.warning(f"OSS 删除失败: {e}")
        return False


# 全局实例
oss_service = OSSService()
