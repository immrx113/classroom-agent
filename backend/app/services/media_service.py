"""媒体校验服务

使用实际容器/编码探测（mutagen）+ WAV 结构解码校验（stdlib wave），
以真实时长落入声明支持范围；不以扩展名或客户端 Content-Type 认定有效（P02-04）。

声明支持的媒体容器/编码（随交付证据包固定）：
- WAV（PCM）
- MP3（MPEG）
- FLAC
- M4A（AAC）
"""

import os
import wave
from dataclasses import dataclass

from mutagen import File as MutagenFile

from app.config import settings
from app.services.errors import ServiceError, ERR_INPUT

# mutagen 类型名 → (container, codec, mime)
_SUPPORTED = {
    "wave": ("wav", "pcm", "audio/wav"),
    "mp3": ("mp3", "mpeg", "audio/mpeg"),
    "flac": ("flac", "flac", "audio/flac"),
    "mp4": ("m4a", "aac", "audio/mp4"),
}


@dataclass(frozen=True)
class MediaInfo:
    container: str
    codec: str
    mime: str
    duration_ms: int
    sample_rate: int | None
    channels: int | None
    size: int


def _reject(message: str) -> None:
    raise ServiceError(status_code=422, code=ERR_INPUT, message=message, retryable=False)


def probe(path: str) -> MediaInfo:
    """校验媒体文件，返回真实探测信息；非法/不可解码/范围外抛出 422。"""
    if not path or not os.path.exists(path):
        _reject("媒体文件不存在")

    size = os.path.getsize(path)
    if size == 0:
        _reject("空文件不是有效音频")

    try:
        mf = MutagenFile(path)
    except Exception:
        mf = None

    if mf is None:
        _reject("无法识别的媒体容器或损坏音频，不是受支持的音频格式")

    kind = type(mf).__name__.lower()
    supported = _SUPPORTED.get(kind)
    if supported is None:
        _reject(f"不支持的媒体容器：{kind}")

    container, codec, mime = supported

    # WAV：额外用 stdlib wave 校验结构可解码（PCM 头 + 数据可读）
    if container == "wav":
        try:
            with wave.open(path, "rb") as w:
                framerate = w.getframerate()
                nframes = w.getnframes()
                if framerate <= 0 or nframes <= 0:
                    _reject("WAV 结构无效或无可解码采样")
        except Exception:
            _reject("WAV 结构损坏或不可解码")

    try:
        length_sec = mf.info.length or 0.0
        sample_rate = getattr(mf.info, "sample_rate", None)
        channels = getattr(mf.info, "channels", None)
    except Exception:
        _reject("无法读取媒体时长信息")

    duration_ms = int(round(float(length_sec) * 1000))
    if duration_ms < settings.MEDIA_MIN_DURATION_MS:
        _reject(f"音频时长过短（{duration_ms}ms < {settings.MEDIA_MIN_DURATION_MS}ms）")
    if duration_ms > settings.MEDIA_MAX_DURATION_MS:
        _reject(f"音频时长超出供应商支持范围（{duration_ms}ms > {settings.MEDIA_MAX_DURATION_MS}ms）")

    return MediaInfo(
        container=container,
        codec=codec,
        mime=mime,
        duration_ms=duration_ms,
        sample_rate=int(sample_rate) if sample_rate else None,
        channels=int(channels) if channels else None,
        size=size,
    )
