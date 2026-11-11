# 01 课程、课时与数据模型

[返回总览](README.md)

## 现状与问题

现有 `meetings/transcripts/summaries/action_items/risks/decisions` 服务于会议。没有课程归属与完整用户范围；转写、摘要及检索索引缺少统一版本关系。`ChatSession.meeting_id` 也没有形成召回范围约束。

涉及现有文件：`backend/app/models/`、`backend/app/schemas/`、`backend/app/api/deps.py`、`backend/app/services/meeting_service.py`、`backend/alembic/versions/`。

## 目标与表设计

下列为逻辑实体。迁移初期允许 Lecture ORM 暂时映射旧 `meetings` 表，但对外接口统一使用 lecture 语义。

| 实体 | 核心字段 | 约束与用途 |
| --- | --- | --- |
| users | id、身份映射 | 预留可靠主体；个人版可配置固定本地主体，不接受客户端随意指定 user_id |
| courses | id、owner_id、title、teacher、term、archived_at | 课程归属；第一版个人课程，暂不做共享角色 |
| lectures | id、course_id、title、lecture_date、active_audio_id、active_transcript_version_id | 一次课时；状态由阶段任务汇总 |
| audio_assets | id、lecture_id、storage_key、checksum、mime、duration_ms、size、version、is_demo | 物理文件与业务记录分离；不同音频不得共用转写版本 |
| transcript_versions | id、audio_asset_id、version、provider、provider_task_id、status、parent_id | 固定 ASR 与纠错输入来源 |
| transcript_segments | id、version_id、seq_index、speaker、content、start_ms、end_ms | 唯一约束 `(version_id, seq_index)`；起止范围合法 |
| note_versions | id、lecture_id、transcript_version_id、pipeline_version、status、content | 草稿、待确认、已发布、过期；保留历史 |
| knowledge_points / worked_examples / learning_tasks | id、lecture_id、note_version_id、结构化字段 | 三类业务独立建模，字段见模块 04 |
| source_documents / source_document_versions | id、course_id、lecture_id、文件与解析版本 | 文件级身份独立于文件名，lecture_id 可空表示课程公共课件 |
| evidence_spans | id、来源类型、来源版本、定位字段、quote、有效性 | 指向原文或文档块，不只保存自然语言引用 |
| artifact_evidence | artifact_type、artifact_id、evidence_id | 一个产物可引用多个来源，设置唯一约束 |
| knowledge_chunks | source_version_id、course_id、lecture_id、content、embedding、定位元数据 | 可由现有 knowledge_documents 演进；活动版本过滤 |

`evidence_spans` 使用可验证的来源外键组合或受约束的分类型结构：录音来源必须有转写版本和片段范围，文档来源必须有文档版本和 block_id。对 `artifact_evidence` 的多态关联实现服务层校验与删除清理；若采用独立关联表，应由外键直接保障，不保留未经约束的悬空引用。

## 服务与接口

新增 `course_service.py`、`lecture_service.py`、`evidence_service.py`，由服务层统一处理归属和版本规则。

| 目标接口 | 行为 |
| --- | --- |
| POST /courses | 创建当前用户所属课程 |
| GET /courses | 分页返回可访问课程与课时统计 |
| POST /courses/{id}/lectures | 创建课时 |
| GET /lectures/{id} | 返回阶段状态、当前有效版本和资产摘要 |
| GET /evidence/{id} | 权限验证后返回原文、定位和来源版本状态 |
| DELETE /courses/{id} | 标记删除并调度关联清理；立即停止检索暴露 |

列表响应统一为 `{items, total, page, page_size}`。`ScopeContext` 由身份和课程归属构建，传入所有读取与检索服务；权限不明时拒绝访问，不回退成全库查询。

## 迁移策略

1. 新增课程、归属和版本结构，补齐旧 ORM 与迁移差异，包括 `transcription_mode`。
2. 旧会议先导入“待整理历史资料”课程，并标记 `legacy_meeting`；不自动宣称历史会议已是课堂。
3. 给旧转写建立初始版本，保留 ID 映射；时间从秒转换为毫秒并检查空值和异常值。
4. 旧纪要作为历史产物保存，不伪造知识点与证据；旧决策保留归档入口。
5. 新增索引与非空约束前，先回填并核对孤儿记录和归属。

## 验收

- 两个用户不能访问彼此课程、录音、证据、聊天和检索结果。
- 个人版固定主体与未来登录身份使用同一范围检查路径。
- 同名课程、同名文件不会误合并或互相删除。
- 转写纠错生成新版本，历史答案仍能读取其原版本证据并显示“已有更新”。
- 新数据库和有旧数据的数据库均能完成迁移。

依赖：模块 10 的迁移验证。下游：其余全部模块。
