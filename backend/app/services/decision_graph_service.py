"""决策图谱服务：写入 + 向量关联 + 检索

Q9 决策：写入时即时向量关联 top-3 相似历史决策（relation_type 暂全填 'relates'）
Q5 决策：决策单独建表 + 向量索引
"""

import uuid
import logging

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.decision import Decision, DecisionOption, DecisionRelation
from app.services.access import accessible_lecture_ids
from app.services.embedding_service import embedding_service
from app.services.scope import ScopeContext

logger = logging.getLogger(__name__)


class DecisionGraphService:
    """决策图谱服务"""

    async def save_decisions(
        self,
        db: AsyncSession,
        meeting_id: uuid.UUID,
        decisions: list[dict],
    ) -> list[Decision]:
        """批量保存决策（含 options + 即时向量关联）

        流程：
            1. 删除该 meeting 的旧决策（cascade 会删 options + relations）
            2. 对每个决策生成 embedding（title + context）
            3. 写 decisions + decision_options
            4. 对每个新决策检索 top-3 相似历史决策，写 decision_relations

        Args:
            db: 数据库会话
            meeting_id: 关联的会议 ID
            decisions: decision_extractor 节点输出的决策列表

        Returns:
            已保存的 Decision ORM 对象列表
        """
        # 1. 删除旧决策（cascade 会删 options 和 relations；先清理证据关联）
        from app.services.evidence_service import evidence_service
        existing_decision_ids = (
            await db.execute(
                select(Decision.id).where(Decision.meeting_id == meeting_id)
            )
        ).scalars().all()
        for did in existing_decision_ids:
            await evidence_service.unlink_artifact(db, "decision", did)
        await db.execute(
            delete(Decision).where(Decision.meeting_id == meeting_id)
        )
        await db.flush()

        saved: list[Decision] = []
        for d in decisions:
            # 2. 生成 embedding（title + context 拼接）
            embed_text = f"{d.get('title', '')} {d.get('context', '')}"
            embedding = await embedding_service.embed_text(embed_text)

            # 3. 写 Decision 主表
            decision = Decision(
                meeting_id=meeting_id,
                title=d["title"],
                context=d.get("context"),
                snippet=d.get("snippet"),
                chosen_option=d.get("chosen"),
                reasons=d.get("reasons"),
                objections=d.get("objections"),
                decided_by=d.get("decided_by"),
                decided_at=d.get("decided_at"),
                confidence=d.get("confidence"),
                embedding=embedding,
            )
            db.add(decision)
            await db.flush()  # 拿到 id

            # 4. 写 Options 方案表
            for opt in d.get("options", []):
                option = DecisionOption(
                    decision_id=decision.id,
                    name=opt.get("name", ""),
                    pros=opt.get("pros"),
                    cons=opt.get("cons"),
                    proposed_by=opt.get("proposed_by"),
                    is_chosen=(opt.get("name") == d.get("chosen")),
                )
                db.add(option)

            # 5. 即时关联 top-3 相似历史决策（Q9 决策）
            if embedding:
                await self._link_similar(db, decision.id, embedding)

            saved.append(decision)

        await db.flush()
        logger.info(
            f"[DecisionGraph] 保存 {len(saved)} 个决策，meeting={meeting_id}"
        )
        return saved

    async def _link_similar(
        self,
        db: AsyncSession,
        decision_id: uuid.UUID,
        embedding: list[float],
        top_k: int = 3,
        threshold: float = 0.7,
    ) -> None:
        """检索 top-3 相似历史决策，写入 decision_relations（双向）

        Q9 决策：MVP 阶段 relation_type 全填 'relates'，
        M2 再细化分类（supersedes / contradicts / evolves）

        双向关联：写入 source→target 和 target→source 两条记录
        避免重复：检查 UniqueConstraint (source_decision_id, target_decision_id)

        Args:
            decision_id: 当前决策 ID（排除自身）
            embedding: 当前决策的向量
            top_k: 检索数量
            threshold: 相似度阈值（cosine_similarity = 1 - cosine_distance）
        """
        try:
            stmt = (
                select(
                    Decision.id,
                    Decision.embedding.cosine_distance(embedding).label("distance"),
                )
                .where(Decision.id != decision_id)
                .order_by("distance")
                .limit(top_k)
            )
            result = await db.execute(stmt)

            # 先查询已存在的关系，避免插入时 UniqueConstraint 冲突
            existing_stmt = select(
                DecisionRelation.source_decision_id,
                DecisionRelation.target_decision_id,
            ).where(
                (DecisionRelation.source_decision_id == decision_id)
                | (DecisionRelation.target_decision_id == decision_id)
            )
            existing_result = await db.execute(existing_stmt)
            existing_pairs = {
                (str(s), str(t)) for s, t in existing_result.all()
            }

            for related_id, distance in result.all():
                # cosine_distance ∈ [0, 2]，相似度 = 1 - distance ∈ [-1, 1]
                similarity = max(0.0, 1.0 - distance)
                if similarity < threshold:
                    continue

                # 双向关联：决策 A → B
                pair_ab = (str(decision_id), str(related_id))
                if pair_ab not in existing_pairs:
                    relation = DecisionRelation(
                        source_decision_id=decision_id,
                        target_decision_id=related_id,
                        relation_type="relates",
                        context=f"向量相似度 {similarity:.2f}",
                        similarity_score=similarity,
                    )
                    db.add(relation)
                    existing_pairs.add(pair_ab)

                # 双向关联：决策 B → A（相同相似度）
                pair_ba = (str(related_id), str(decision_id))
                if pair_ba not in existing_pairs:
                    reverse_relation = DecisionRelation(
                        source_decision_id=related_id,
                        target_decision_id=decision_id,
                        relation_type="relates",
                        context=f"向量相似度 {similarity:.2f}",
                        similarity_score=similarity,
                    )
                    db.add(reverse_relation)
                    existing_pairs.add(pair_ba)
        except Exception as e:
            logger.warning(f"[DecisionGraph] 关联失败（不影响决策保存）: {e}")

    async def search(
        self,
        db: AsyncSession,
        query: str,
        top_k: int = 5,
        scope: ScopeContext | None = None,
    ) -> list[dict]:
        """决策语义检索（供 AI 对话 RAG 召回）

        Args:
            query: 查询文本
            top_k: 返回数量

        Returns:
            决策列表，每项含 id / title / context / chosen_option / meeting_id / score
        """
        embedding = await embedding_service.embed_text(query)
        if not embedding:
            logger.warning("[DecisionGraph] 查询向量化失败，返回空列表")
            return []

        try:
            stmt = select(
                Decision,
                Decision.embedding.cosine_distance(embedding).label("distance"),
            )
            if scope is not None:
                stmt = stmt.where(
                    Decision.meeting_id.in_(accessible_lecture_ids(scope))
                )
            stmt = stmt.order_by("distance").limit(top_k)
            result = await db.execute(stmt)
            return [
                {
                    "id": str(d.id),
                    "title": d.title,
                    "context": d.context,
                    "chosen_option": d.chosen_option,
                    "meeting_id": str(d.meeting_id) if d.meeting_id else None,
                    "score": max(0.0, 1.0 - distance),
                    "source_type": "decision",
                }
                for d, distance in result.all()
            ]
        except Exception as e:
            logger.error(f"[DecisionGraph] 检索失败: {e}")
            return []

    async def list_decisions(
        self,
        db: AsyncSession,
        meeting_id: uuid.UUID | None = None,
        skip: int = 0,
        limit: int = 20,
        scope: ScopeContext | None = None,
    ) -> tuple[list[Decision], int]:
        """决策列表（含分页 + 按 meeting 筛选；传入 scope 时仅返回可访问课时决策）"""
        stmt = select(Decision)
        cond = None
        if meeting_id:
            cond = Decision.meeting_id == meeting_id
        elif scope is not None:
            cond = Decision.meeting_id.in_(accessible_lecture_ids(scope))
        if cond is not None:
            stmt = stmt.where(cond)
        stmt = stmt.order_by(Decision.created_at.desc()).offset(skip).limit(limit)
        result = await db.execute(stmt)
        decisions = list(result.scalars().all())

        count_stmt = select(func.count(Decision.id))
        if cond is not None:
            count_stmt = count_stmt.where(cond)
        total = (await db.execute(count_stmt)).scalar_one()
        return decisions, total

    async def get_decision(
        self,
        db: AsyncSession,
        decision_id: uuid.UUID,
        scope: ScopeContext | None = None,
    ) -> dict | None:
        """决策详情（含 options + 关联决策）

        scope 非空时：主决策须属于可访问课时（无 meeting_id 的全局决策不放宽），
        关联决策同样按可访问课时过滤，避免越权关联详情外泄。
        """
        result = await db.execute(
            select(Decision).where(Decision.id == decision_id)
        )
        decision = result.scalar_one_or_none()
        if not decision:
            return None

        # 主决策自身可访问性
        if scope is not None:
            if decision.meeting_id is None:
                return None
            own = await db.execute(
                select(Decision.id).where(
                    Decision.id == decision_id,
                    Decision.meeting_id.in_(accessible_lecture_ids(scope)),
                )
            )
            if own.scalar_one_or_none() is None:
                return None

        # options
        opts_result = await db.execute(
            select(DecisionOption)
            .where(DecisionOption.decision_id == decision_id)
            .order_by(DecisionOption.created_at)
        )
        options = list(opts_result.scalars().all())

        # 关联决策（双向查询：作为 source 和作为 target），可选按可访问课时过滤
        related_cond = None
        if scope is not None:
            related_cond = Decision.meeting_id.in_(accessible_lecture_ids(scope))

        # 1. 作为 source → target
        rel_stmt = (
            select(DecisionRelation, Decision)
            .join(Decision, DecisionRelation.target_decision_id == Decision.id)
            .where(DecisionRelation.source_decision_id == decision_id)
        )
        if related_cond is not None:
            rel_stmt = rel_stmt.where(related_cond)
        rel_result = await db.execute(
            rel_stmt.order_by(DecisionRelation.similarity_score.desc().nullslast())
        )

        # 2. 作为 target ← source
        reverse_stmt = (
            select(DecisionRelation, Decision)
            .join(Decision, DecisionRelation.source_decision_id == Decision.id)
            .where(DecisionRelation.target_decision_id == decision_id)
        )
        if related_cond is not None:
            reverse_stmt = reverse_stmt.where(related_cond)
        reverse_rel_result = await db.execute(
            reverse_stmt.order_by(DecisionRelation.similarity_score.desc().nullslast())
        )
        
        # 合并去重（避免 A→B 和 B→A 重复显示）
        related_map = {}
        for r, d in rel_result.all():
            related_map[str(d.id)] = {
                "id": str(d.id),
                "title": d.title,
                "similarity_score": r.similarity_score,
                "relation_type": r.relation_type,
                "context": r.context,
            }
        for r, d in reverse_rel_result.all():
            related_map[str(d.id)] = {
                "id": str(d.id),
                "title": d.title,
                "similarity_score": r.similarity_score,
                "relation_type": r.relation_type,
                "context": r.context,
            }
        
        # 按相似度排序
        related = sorted(
            list(related_map.values()),
            key=lambda x: x["similarity_score"] or 0,
            reverse=True,
        )

        return {
            "id": str(decision.id),
            "meeting_id": str(decision.meeting_id) if decision.meeting_id else None,
            "title": decision.title,
            "context": decision.context,
            "snippet": decision.snippet,
            "chosen_option": decision.chosen_option,
            "reasons": decision.reasons,
            "objections": decision.objections,
            "decided_by": decision.decided_by,
            "decided_at": decision.decided_at.isoformat() if decision.decided_at else None,
            "confidence": decision.confidence,
            "created_at": decision.created_at.isoformat() if decision.created_at else None,
            "options": [
                {
                    "id": str(o.id),
                    "name": o.name,
                    "pros": o.pros,
                    "cons": o.cons,
                    "proposed_by": o.proposed_by,
                    "is_chosen": o.is_chosen,
                }
                for o in options
            ],
            "related_decisions": related,
        }


# 全局实例
decision_graph_service = DecisionGraphService()
