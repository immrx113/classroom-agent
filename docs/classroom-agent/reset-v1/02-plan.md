# 课堂 Agent 修改方案 · v1.0
2026-11-10 ｜ 对应[需求](01-requirements.md)与[验收项](03-acceptance.md)

## 从当前代码出发
后端同时存在 meeting_service、summary_service 和 course_service、lecture_service、organize_service。前端 features 同时保留 meetings、decisions、notes、retrieval。历史 phase-01 至 phase-04 与 checkpoint 专项已有验收材料。文件存在只能作为复用线索，实施时须从实际页面、API 和工作流追踪调用。以下步骤按依赖顺序实施。

|步骤|修改位置与做法|交付门槛|
|---|---|---|
|1 建立基线|从 frontend/src/router 和 backend/app 的路由注册列出活跃入口；用隔离数据库备份副本运行 backend/scripts 下的 fresh 与 olddata 迁移检查。记录旧记录数量、归属及可读性，冻结课程和课时标识。|A1、A7 的迁移前后对照|
|2 接通输入|沿用 course_service、lecture_service、audio_asset_service、transcription_service 与 document_parser。上传时绑定课时，向转写段、课件分块传递稳定的来源标识。|A1、A2|
|3 对齐编排|以 organize_service 为课堂业务入口核查实际调用。沿用原 LangGraph 的 Planner → Budget Check → 动态分支 → Output Validator → Review。纪要分支改笔记，行动项改例题，风险改重点难点，决策提取改知识点。由服务层负责发布、落库与索引交接；以运行及版本标识消除重复写入。|A3、A4、A6|
|4 对齐问答|在 retrieval_service 两路召回前应用相同的课程、课时、权限和发布约束；融合去重后由 chat_service 流式返回答案与来源标识。前端复用资料预览和音频定位。|A5|
|5 切换入口|在现有 React 路由串联课程列表、课时详情、处理进度、草稿确认与问答。先在数据副本完成全链路，再切换默认入口。记录旧入口映射，验收失败时恢复原路由配置；数据库回退先在副本演练。|A1 至 A7 全部通过|

保留 FastAPI、LangGraph、PostgreSQL/pgvector 与现有模型接入层。复用现有预算、检查点和作业服务前，核查它们在课堂入口是否生效。新增表或字段先做兼容迁移，验证历史数据后再考虑清理旧实现。

2026-11-10 起各阶段采用 [v1.1 冻结清单](03-acceptance-v1.1.md)。[阶段 2 独立复验](12-phase2-review-v1.1.md)已通过。

2026-11-11 验收更新：用户确认无历史会议数据，现采用 [v1.2 冻结清单](03-acceptance-v1.2.md)。[最终独立验收](17-final-review-v1.2.md)已通过；本文此前的阻塞结论作为历史记录。
