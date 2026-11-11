"""对话服务：双路 RAG 检索增强 + LLM 流式生成

Q5 决策：AI 对话采用双路召回（文档 + 决策）+ RRF 融合
"""

import logging
import uuid
from typing import AsyncGenerator, Optional

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chat import ChatSession, ChatMessage
from app.services.access import chat_session_accessible_cond
from app.services.scope import ScopeContext
from app.services.embedding_service import EmbeddingProvider
from app.services.knowledge_service import knowledge_service
from app.services.decision_graph_service import decision_graph_service
from app.services.retrieval_service import retrieval_service

logger = logging.getLogger(__name__)

# 课堂问答来源标签（R5/A5：引用能定位来源）
_CLASSROOM_SOURCE_LABEL = {
    "transcript": "转写",
    "document": "课件",
    "note": "笔记",
}

# 证据不足时的确定性说明（R5：证据不足时明确说明，不编造）
CLASSROOM_INSUFFICIENT_TEXT = (
    "依据不足：在当前课程/课时范围内未检索到与问题相关的转写、课件或笔记，"
    "无法给出可靠回答。请补充相关材料，或换一种问法。"
)

# 测试注入：课堂问答替身。生产恒 None → 走真实 LLM 流式（或确定性摘录）。
_classroom_answerer_fn = None

# 系统提示词
SYSTEM_PROMPT = """你是 AI 会议助手，专注于企业会议场景的智能问答。

你的能力：
1. 基于知识库与决策库的检索结果回答会议相关问题
2. 总结会议要点、解释行动项、识别风险
3. 回答历史评审中做过的决策、对比相似决策
4. 当用户问题与会议/知识库无关时，礼貌引导回会议主题

回答规范：
- 优先使用【知识库】和【决策库】中的内容回答
- 引用知识时标注来源（如"根据会议纪要..."、"根据历史决策..."）
- 回答决策相关问题时，说明已选方案、候选方案与选择理由
- 使用 Markdown 格式输出，结构清晰
- 如检索结果为空或不相关，基于通用知识回答但需说明

当前检索结果：
{context}
"""


class ChatService:
    """对话服务"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        ) if settings.OPENAI_API_KEY else None
        self.model = settings.LLM_MODEL

    async def create_session(
        self,
        db: AsyncSession,
        owner_id: str,
        meeting_id: uuid.UUID | None = None,
        title: str | None = None,
    ) -> ChatSession:
        """创建对话会话"""
        session = ChatSession(
            owner_id=owner_id,
            meeting_id=meeting_id,
            title=title or "新对话",
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    async def list_sessions(
        self,
        db: AsyncSession,
        meeting_id: uuid.UUID | None = None,
        scope: ScopeContext | None = None,
    ) -> list[ChatSession]:
        """获取会话列表；传入 scope 时仅返回本主体会话（全局或可访问课时会话）"""
        stmt = select(ChatSession).order_by(ChatSession.created_at.desc())
        if meeting_id:
            stmt = stmt.where(ChatSession.meeting_id == meeting_id)
        elif scope is not None:
            stmt = stmt.where(chat_session_accessible_cond(scope))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_session(
        self, db: AsyncSession, session_id: uuid.UUID
    ) -> ChatSession | None:
        """获取会话（用于归属校验）"""
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        return result.scalar_one_or_none()

    async def get_session_messages(
        self,
        db: AsyncSession,
        session_id: str,
    ) -> list[ChatMessage]:
        """获取会话消息历史"""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def save_message(
        self,
        db: AsyncSession,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> ChatMessage:
        """保存消息"""
        msg = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            metadata=metadata,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        return msg

    async def chat_stream(
        self,
        db: AsyncSession,
        session_id: str,
        query: str,
        images: list[str] | None = None,
        scope: ScopeContext | None = None,
    ) -> AsyncGenerator[str, None]:
        """流式对话：RAG 检索 + LLM 流式生成

        Args:
            images: base64 编码的图片列表（data URL），用于多模态对话
        通过 SSE 逐步返回内容。
        """
        if not self.client:
            yield "error: LLM 未配置"
            return

        # 1. 保存用户消息（含图片元信息）
        await self.save_message(
            db,
            session_id,
            "user",
            query,
            metadata={"images": images} if images else None,
        )

        # 2. 获取历史消息（最近 10 条）
        history = await self.get_session_messages(db, session_id)
        history_msgs = [
            {"role": m.role, "content": m.content}
            for m in history[-10:]  # 最近 10 条
        ]

        # 3. 查询改写：多轮对话中消解指代（"它"、"这个"等）
        # 仅当存在历史且查询简短时触发，节省 token
        rewritten_query = await self._rewrite_query(query, history_msgs[:-1]) if len(history_msgs) > 1 else query
        logger.info(f"查询改写: {query!r} → {rewritten_query!r}")

        # 4. 双路 RAG 检索（文档 + 决策）+ RRF 融合
        # 召回必须受范围约束：不把其他主体的知识/决策喂进上下文
        try:
            doc_results = await knowledge_service.search(db, rewritten_query, top_k=3, scope=scope)
        except Exception as e:
            logger.warning(f"知识库检索失败: {e}")
            doc_results = []

        try:
            decision_results = await decision_graph_service.search(db, rewritten_query, top_k=3, scope=scope)
        except Exception as e:
            logger.warning(f"决策库检索失败: {e}")
            decision_results = []

        # RRF 融合两路结果（跨来源统一排序）
        fused = self._rrf_fuse(doc_results, decision_results, top_k=5)

        # 构建上下文（区分来源类型）
        if fused:
            context_parts = []
            for i, r in enumerate(fused, 1):
                source_type = r.get("source_type", "")
                if source_type == "decision":
                    title = r.get("title", "未知决策")
                    chosen = r.get("chosen_option")
                    context_text = r.get("context", "")
                    parts = [f"[{i}] 来源：决策库 - {title}"]
                    if chosen:
                        parts.append(f"已选方案：{chosen}")
                    if context_text:
                        parts.append(f"背景：{context_text[:400]}")
                    context_parts.append("\n".join(parts))
                else:
                    source = "会议纪要" if source_type == "meeting_summary" else "文档"
                    context_parts.append(
                        f"[{i}] 来源：{source} - {r.get('title', '未知')}\n"
                        f"内容：{r.get('content', '')[:500]}"
                    )
            context = "\n\n".join(context_parts)
        else:
            context = "（未检索到相关知识或决策）"

        # 5. 构建消息列表
        # 有图片时使用多模态模型 qwen-vl-plus
        has_images = bool(images)
        model = settings.VISION_MODEL if has_images else self.model

        if has_images:
            # 多模态：当前用户消息包含文本 + 图片
            user_content: list[dict] = [{"type": "text", "text": query}]
            for img in images:
                user_content.append({"type": "image_url", "image_url": {"url": img}})

            messages = [
                {"role": "system", "content": SYSTEM_PROMPT.format(context=context)},
                *[
                    {"role": m["role"], "content": m["content"]}
                    for m in history_msgs[:-1]  # 排除当前用户消息（已含图片）
                ],
                {"role": "user", "content": user_content},
            ]
        else:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT.format(context=context)},
                *history_msgs,
            ]

        # 6. 流式生成
        full_response = ""
        llm_failed = False
        try:
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                temperature=0.7,
            )

            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    full_response += delta
                    yield delta

        except Exception as e:
            logger.error(f"LLM 流式生成失败: {e}")
            # 分类错误并返回友好提示，避免暴露 request_id 等技术细节
            from app.agents.meeting_graph import _classify_llm_error
            friendly_msg = _classify_llm_error(e)
            llm_failed = True
            yield friendly_msg

        # 7. 保存助手回复（仅当未失败且内容非空时）
        # 失败时不保存，避免污染历史对话
        if full_response and not llm_failed:
            await self.save_message(
                db,
                session_id,
                "assistant",
                full_response,
                metadata={
                    "sources": [
                        {
                            "title": r.get("title"),
                            "source_type": r.get("source_type"),
                            "score": r.get("rrf_score", r.get("rerank_score", r.get("score"))),
                        }
                        for r in fused
                    ]
                } if fused else None,
            )

    # ── 课堂问答（R5/A5：范围检索两路召回 → 融合/去重/重排 → 流式答案 + 来源标识） ──
    async def classroom_chat_stream(
        self,
        db: AsyncSession,
        query: str,
        scope: ScopeContext,
        top_k: Optional[int] = None,
        embed: Optional[EmbeddingProvider] = None,
    ) -> AsyncGenerator[dict, None]:
        """课堂问答流式事件（dict，由 SSE 端点逐条序列化）。

        产出事件：
        - {"type": "sources", "status": ..., "results": [...], "trace": {...}}  检索来源标识
        - {"type": "token", "content": ...}                                     答案增量
        - {"type": "done", "status": ...}                                       结束
        证据不足（no_evidence / retrieval_error）时仅返回确定性「依据不足」说明，不调用 LLM 编造。
        """
        result = await retrieval_service.search(
            db, scope, query, top_k=top_k, embed=embed
        )
        status = result["status"]
        results = result["results"]
        trace = result["trace"]

        yield {"type": "sources", "status": status, "results": results, "trace": trace}

        if status in ("no_evidence", "retrieval_error"):
            yield {"type": "token", "content": CLASSROOM_INSUFFICIENT_TEXT}
            yield {"type": "done", "status": status}
            return

        full = ""
        answerer = _classroom_answerer_fn
        if answerer is not None:
            async for tok in answerer(query, results):
                full += tok
                yield {"type": "token", "content": tok}
        elif self.client is not None and settings.CLASSROOM_PAID_CALLS_ENABLED:
            async for tok in self._classroom_llm_stream(query, results):
                full += tok
                yield {"type": "token", "content": tok}
        else:
            # 无 LLM / 未开启付费调用：确定性摘录，便于自动验收核对引用对应原文
            full = self._classroom_deterministic_answer(results)
            yield {"type": "token", "content": full}

        yield {"type": "done", "status": status}

    async def _classroom_llm_stream(
        self, query: str, results: list[dict]
    ) -> AsyncGenerator[str, None]:
        """真实 LLM 流式：仅基于检索材料回答，引用来源编号，不足时明确说明。"""
        context_parts = []
        for i, r in enumerate(results, 1):
            label = self._classroom_source_label(r.get("source_type"))
            text = "\n".join(r.get("content") or [])
            context_parts.append(f"[{i}] 来源：{label}\n{text[:800]}")
        context = "\n\n".join(context_parts) or "（未检索到内容）"
        system = (
            "你是课堂学习助手。仅基于给定的检索材料回答用户问题，引用来源编号（如「[1]」）。"
            "材料不足时明确说明依据不足，不要编造。使用 Markdown 输出。\n\n检索材料：\n" + context
        )
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": query},
            ],
            stream=True,
            temperature=0.3,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def _classroom_deterministic_answer(self, results: list[dict]) -> str:
        """确定性摘录（无 LLM / 测试）：逐条标注来源，便于核对引用对应原文。"""
        parts = []
        for i, r in enumerate(results, 1):
            label = self._classroom_source_label(r.get("source_type"))
            text = "\n".join(r.get("content") or [])[:300]
            parts.append(f"[{i}] 来源：{label}｜{text}")
        if not parts:
            return CLASSROOM_INSUFFICIENT_TEXT
        return "依据检索到的课堂材料回答：\n" + "\n\n".join(parts)

    @staticmethod
    def _classroom_source_label(source_type: str) -> str:
        return _CLASSROOM_SOURCE_LABEL.get(source_type, source_type)

    def _rrf_fuse(
        self,
        doc_results: list[dict],
        decision_results: list[dict],
        top_k: int = 5,
        k: int = 60,
    ) -> list[dict]:
        """RRF 融合文档与决策两路检索结果

        公式：score = 1 / (k + rank)
        两路结果 id 来自不同表（knowledge_documents / decisions），不会冲突，
        RRF 的作用是跨来源统一排序——决定哪条文档/决策应排在前面。

        Args:
            doc_results: knowledge_service.search 返回的结果（已按相关性排序）
            decision_results: decision_graph_service.search 返回的结果（已按相似度排序）
            top_k: 融合后返回数量
            k: RRF 平滑常数（标准值 60）

        Returns:
            融合后的结果列表，每项含原始字段 + rrf_score
        """
        scores: dict[str, float] = {}
        items_map: dict[str, dict] = {}

        # 文档路：按返回顺序赋 rank（search 已内部排序）
        for rank, item in enumerate(doc_results):
            key = f"doc:{item.get('id')}"
            scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
            items_map[key] = item

        # 决策路
        for rank, item in enumerate(decision_results):
            key = f"decision:{item.get('id')}"
            scores[key] = scores.get(key, 0) + 1.0 / (k + rank + 1)
            items_map[key] = item

        # 按融合分数降序排序
        sorted_keys = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        result = []
        for key in sorted_keys[:top_k]:
            item = items_map[key].copy()
            item["rrf_score"] = round(scores[key], 4)
            result.append(item)
        return result

    async def _rewrite_query(self, query: str, history: list[dict]) -> str:
        """查询改写：消解多轮对话中的指代词

        将依赖上下文的查询（如"它的截止日期是什么时候？"）改写为独立查询
        （如"行动项『完成产品需求文档』的截止日期是什么时候？"）。

        仅在查询简短（可能含指代）时触发，避免不必要的 LLM 调用。
        """
        # 简单启发式：查询过短或包含指代词时才改写
        if len(query) > 50 or not history:
            return query

        # 检测指代词（中英文）
        ref_keywords = ["它", "他", "她", "这个", "那个", "这些", "那些", "其",
                        "it", "this", "that", "these", "those", "they", "them"]
        query_lower = query.lower()
        if not any(kw in query_lower for kw in ref_keywords):
            return query

        rewrite_prompt = [
            {"role": "system", "content": (
                "你是查询改写助手。根据对话历史，将用户最新查询中的指代词替换为具体内容，"
                "使其成为不依赖上下文也能理解的独立查询。\n"
                "只输出改写后的查询，不要解释、不要引号。如果无需改写，原样输出。"
            )},
            *[
                {"role": m["role"], "content": m["content"][:200] if isinstance(m["content"], str) else str(m["content"])[:200]}
                for m in history[-4:]  # 最近 4 条上下文
            ],
            {"role": "user", "content": f"请改写此查询: {query}"},
        ]

        try:
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=rewrite_prompt,
                temperature=0.0,
                max_tokens=100,
            )
            rewritten = resp.choices[0].message.content.strip()
            # 防御：避免模型返回空或过长解释
            if rewritten and len(rewritten) < 200:
                return rewritten
            return query
        except Exception as e:
            logger.warning(f"查询改写失败，使用原始查询: {e}")
            return query

    async def delete_session(self, db: AsyncSession, session_id: str) -> bool:
        """删除会话"""
        stmt = select(ChatSession).where(ChatSession.id == session_id)
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()
        if session:
            await db.delete(session)
            await db.commit()
            return True
        return False


chat_service = ChatService()
