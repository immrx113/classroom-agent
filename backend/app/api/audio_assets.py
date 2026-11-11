"""音频资产受控读取（Range 播放）"""

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_scope
from app.services.audio_asset_service import audio_asset_service
from app.services.scope import ScopeContext

router = APIRouter(prefix="/audio-assets", tags=["音频资产"])

_CHUNK = 64 * 1024


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):]
    if "," in spec:
        return None
    spec = spec.strip()
    try:
        if spec.startswith("-"):
            length = int(spec[1:])
            if length <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
        else:
            a, _, b = spec.partition("-")
            start = int(a)
            end = int(b) if b else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _iter_range(path: str, start: int, end: int):
    remaining = end - start + 1
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            data = f.read(min(_CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


@router.get("/{asset_id}/content")
async def get_audio_content(
    asset_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    scope: ScopeContext = Depends(get_scope),
):
    """授权播放：MIME 与实际媒体一致，支持 Range 读取。"""
    path, mime, size = await audio_asset_service.resolve_asset(db, scope, asset_id)

    range_header = request.headers.get("range")
    if range_header:
        parsed = _parse_range(range_header, size)
        if parsed is None:
            return StreamingResponse(
                iter(()),
                status_code=416,
                headers={"Content-Range": f"bytes */{size}"},
            )
        start, end = parsed
        return StreamingResponse(
            _iter_range(path, start, end),
            status_code=206,
            media_type=mime,
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
            },
        )

    return FileResponse(path, media_type=mime)
