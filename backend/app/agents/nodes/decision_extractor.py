"""decision_extractor 节点：组合 DecisionDetector + OptionExtractor 两步流水线

Q8 决策：作为新增并行节点接入 meeting_graph_v2，与 summary/action_items/risks 同级

失败不阻塞主流程：返回 errors 累加，decisions 为空数组
"""

import logging

from app.agents.nodes.decision_detector import detect_decisions
from app.agents.nodes.option_extractor import extract_options

logger = logging.getLogger(__name__)


async def decision_extractor_node(state: dict) -> dict:
    """评审决策抽取节点（两步流水线）

    流程：
        transcript_text
            ↓
        Step 1: detect_decisions (全量扫描定位决策段)
            ↓
        Step 2: extract_options (逐段抽取结构化选项)
            ↓
        list[dict] → state["decisions"]

    Returns:
        {"decisions": [...]} 或 {"decisions": [], "errors": [...]}
    """
    # 优先用压缩后文本（如果 budget_check 节点压缩了）
    transcript = (
        state.get("transcript_compressed_text") or state.get("transcript_text", "")
    )
    if not transcript:
        logger.warning("[decision_extractor] 无转写文本，跳过")
        return {"decisions": []}

    try:
        # Step 1: 全量扫描定位决策段
        segments = await detect_decisions(transcript)
        if not segments:
            logger.info("[decision_extractor] 未识别到决策段")
            return {"decisions": []}

        # Step 2: 逐段抽取结构化选项
        decisions: list[dict] = []
        # 从会议状态获取决策时间（parse ISO 字符串）
        from datetime import datetime
        meeting_date = None
        meeting_date_str = state.get("meeting_date")
        if meeting_date_str:
            try:
                meeting_date = datetime.fromisoformat(meeting_date_str)
            except (ValueError, TypeError):
                pass  # 解析失败则为 None
        
        for seg in segments:
            try:
                extracted = await extract_options(seg, transcript)
                if extracted:
                    decision_dict = extracted.model_dump(by_alias=True)
                    # 补充 decided_at（使用会议时间）
                    if meeting_date:
                        decision_dict["decided_at"] = meeting_date
                    decisions.append(decision_dict)
            except Exception as e:
                # 单个段抽取失败不影响其他段
                logger.warning(
                    f"[decision_extractor] 段抽取失败，跳过: {e} | snippet={seg.snippet[:50]!r}"
                )

        logger.info(
            f"[decision_extractor] 识别 {len(segments)} 段，成功抽取 {len(decisions)} 个决策"
        )
        return {"decisions": decisions}

    except Exception as e:
        # 整体失败不阻塞主流程（纪要/行动项/风险照常生成）
        logger.error(f"[decision_extractor] 失败（不阻塞主流程）: {e}")
        return {"decisions": [], "errors": [f"decision_extractor: {str(e)}"]}
