"""知识库 API 路由（第四阶段：停用旧 title 基写入/检索入口）

第一阶段原型知识库按 title 归属、无课程/课时范围过滤，不能作为本轮检索入口。
本文件统一显式停用（410 Gone），课程化课件与范围检索请用：
- 上传/列表/删除：POST/GET/DELETE /api/courses/{course_id}/documents、/api/documents/{id}
- 范围检索：POST /api/retrieval/search
"""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/knowledge", tags=["知识库(已停用)"])

_GONE = (
    "知识库旧入口已停用：文档请走 /api/courses/{course_id}/documents，"
    "范围检索请走 POST /api/retrieval/search。"
)


@router.post("/index")
async def index_text():
    raise HTTPException(status_code=410, detail=_GONE)


@router.post("/upload")
async def upload_document():
    raise HTTPException(status_code=410, detail=_GONE)


@router.post("/search")
async def search_knowledge():
    raise HTTPException(status_code=410, detail=_GONE)


@router.get("/documents")
async def list_documents():
    raise HTTPException(status_code=410, detail=_GONE)


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id):
    raise HTTPException(status_code=410, detail=_GONE)
