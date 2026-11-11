# 阶段 1 交接说明 · 现状与数据基线（reset-v1）

日期：2026-11-10 ｜ 执行人：Claude（deepseek-v4-pro）｜ 范围：仅阶段 1，未做任何业务代码改动。

> **返工记录（阶段 1 复验）**：按独立验收返工两项 —— ① 修复 `verify_fresh_migration_phase1.py` 的 head 断言（改从 Alembic 迁移目录动态读取预期 head，保留版本不一致时的失败能力）；② 重写本节第 7 节为一段可从仓库根目录直接运行的命令。均未改动业务代码或冻结判据。

---

## 1. 本阶段范围

按 `01-requirements.md` / `02-plan.md` / `03-acceptance.md` 的「步骤 1 建立基线」，从默认页面与后端路由追踪**会议**与**课堂**两套入口，列出各自调用的服务与读写对象；核对新库建表与旧库副本迁移；记录课程、课时、历史会议的标识/数量/归属/可读性；输出现状表、迁移验证结果与后续改动清单。

本阶段为**纯现状核查 + 隔离副本迁移验证**，不改业务代码，不触碰工作区已有改动。

---

## 2. 工作区 / 版本标识

- 仓库：`C:\Users\11322\Desktop\cs\project\yuan-meeting-main`（**非 Git 工作树**，无法给提交号；以 alembic 迁移版本作为版本标识）。
- 后端 Alembic **head = `f1a2b3c4d5e6`**（`add_subject_audit_and_reuse`）。
- 活跃配置 `backend/.env`：`DATABASE_URL=.../yuan_classroom_local`，`LOCAL_SUBJECT_ID=local-user`（默认值），`ASR_MODE=real`，`CLASSROOM_PAID_CALLS_ENABLED=true`。
- 基础设施：`docker compose up -d`（`yuan_meet_postgres:5432` + `yuan_meet_redis:6379`），本轮已拉起。

---

## 3. 修改文件

- `backend/scripts/verify_fresh_migration_phase1.py`（返工项 1）：head 断言由硬编码 `f7a8b9c0d1e2` 改为经 `ScriptDirectory.from_config(Config("alembic.ini")).get_heads()` 动态读取当前迁移目录 head，与 `alembic_version` 表实际版本比较；版本不一致时仍记 FAIL 并退出非 0。
- 本交接说明（第 7 节重写，返工项 2）。
- 未改动任何业务代码、模型、迁移文件或冻结判据。

---

## 4. 现状表：两套入口 → 服务 → 读写对象

### 4.1 前端入口（`frontend/src/router/routes.tsx`）

默认首页仍是 **会议/课时列表页**（`index: true → MeetingListPage`，侧栏文案已改为「课时管理」）。已存在的课堂页面只有两处：`/retrieval`（课堂检索）、`/notes/:noteId`（笔记详情）。**缺少课程列表页、课时详情页、课堂主页**。

| 前端路由 | 页面 | 说明 |
|---|---|---|
| `/`（默认） | `MeetingListPage` | 复用 `createMeeting`（旧 `/meetings`），新建的是历史资料课程下的 legacy 课时，非真正的课程/课时流程 |
| `/meetings/:id` | `MeetingDetailPage` | 旧会议详情；其上传按钮调 `POST /meetings/{id}/upload`，该后端入口已 **410 停用** |
| `/summaries`、`/summaries/:id` | Summary 列表/详情 | 旧纪要语义（`summary_service`） |
| `/decisions`、`/decisions/:id` | Decision 列表/详情 | 旧决策语义 |
| `/chat` | ChatPage | SSE 问答，仍走**旧 RAG**（见 §4.3） |
| `/knowledge` | KnowledgePage | 旧知识库 |
| `/agent-runs`、`/agent-runs/:id` | AgentRun 列表/详情 | 运行记录 |
| `/retrieval` | RetrievalPage | **课堂检索**（新链路，含课程/课时范围、课件上传、回听定位） |
| `/notes/:noteId` | NoteDetailPage | 课堂笔记详情 |

### 4.2 后端路由聚合（`backend/app/api/__init__.py`）

注册顺序：health、meetings、summaries(global+sub)、knowledge、chat、agent_runs、rooms、decisions、**courses、lectures、evidence、audio_assets、jobs、notes、documents、retrieval**、diagnostics。

### 4.3 会议（旧）入口调用链

| 入口 | 服务 | 读写对象（模型/表） |
|---|---|---|
| `POST/GET/PATCH/DELETE /api/meetings` | `meeting_service` + `course_service.ensure_history_course` | `Meeting`(=meetings 表，兼容期即课时)、`Transcript`(旧 transcripts 表)；新建归入「历史导入」课程并 `legacy_meeting=True` |
| `POST /api/meetings/{id}/upload` | — | **已停用，返回 410**（P02-01） |
| `GET /api/meetings/{id}/transcripts`、`transcription-status` | `meeting_service` | `Transcript`（旧表） |
| `/api/summaries` | `summary_service`（LangGraph `meeting_graph_v2`） | `Meeting`、`Summary`、`ActionItem`、`Risk`、`Transcript` |
| `/api/decisions` | `decision_graph_service` | `Decision`、`DecisionOption`、`DecisionRelation` |
| `/api/chat`（SSE） | `chat_service` → `knowledge_service.search` + `decision_graph_service.search` | `ChatSession`、`ChatMessage`、`KnowledgeDocument`、`Decision*`。**注意：问答仍是旧「文档+决策」双路 RAG，未接课堂检索** |
| `/api/knowledge` | `knowledge_service` | `KnowledgeDocument` |
| `/api/agent-runs` | `agent_run_service` | `AgentRun` |

### 4.4 课堂（新）入口调用链

| 入口 | 服务 | 读写对象 |
|---|---|---|
| `POST/GET /api/courses`、`GET/POST /api/courses/{id}/lectures`、`DELETE /api/courses/{id}` | `course_service` | `Course`、`Meeting` |
| `POST /api/courses/{id}/index` | `index_service` | `IndexVersion`、`IndexBlock`、`ProcessingJob`、`OutboxEvent` 等 |
| `GET /api/lectures/{id}`、`index-status`、`POST /api/lectures/{id}/audio` | `lecture_service`、`audio_asset_service`、`index_service` | `Meeting`、`AudioAsset`、`TranscriptVersion`、`TranscriptSegment`、`ProcessingJob`、`OutboxEvent` |
| `POST /api/lectures/{id}/organize`、`GET .../notes`、`PATCH /api/notes/{id}`、`POST .../publish` | `organize_service` | `Meeting`、`NoteVersion`、`ChunkManifest`、`ChunkItem`、`IndexIntent`、`ProcessingJob`、`OutboxEvent`、`JobCheckpoint`、`TranscriptVersion`、`TranscriptSegment`、`ArtifactEvidence`、`EvidenceSpan` |
| `POST /api/retrieval/search` | `retrieval_service` | `IndexBlock`、`IndexVersion`、`Meeting`、`NoteVersion`、`ParsedBlock`、`SourceDocument`、`SourceDocumentVersion`、`TranscriptSegment`、`TranscriptVersion` |
| `POST/GET/DELETE /api/courses/{id}/documents`、`/api/documents/{id}*` | `document_service` | `SourceDocument`、`SourceDocumentVersion`、`ParsedBlock`、`IndexBlock`、`Meeting`、`Course` |
| `GET /api/audio-assets/{id}/content`（Range 播放） | `audio_asset_service` | `AudioAsset` |
| `GET/POST /api/jobs/{id}*` | `job_service` | `ProcessingJob`、`OutboxEvent`、`AudioAsset` |
| `/api/evidence` | `evidence_service` | `EvidenceSpan`、`ArtifactEvidence`、`NoteVersion`、`Transcript*`、`ParsedBlock` 等 |

### 4.5 权限模型（关键现状）

- 身份固定：`ScopeContext(subject_id=settings.LOCAL_SUBJECT_ID)`，`get_scope()` 恒返回 `"local-user"`（`app/services/scope.py`、`app/api/deps.py`）。
- 统一访问控制 `app/services/access.py`：`owner_id == subject_id` + `archived_at IS NULL` + 课程/课时范围约束；越权一律 404（不泄露存在性）。
- **HTTP 层没有多用户身份**：验收 A1/A5 的「两个互无权限用户 / 跨用户隔离」无法通过 HTTP 直接制造第二个用户，需在服务层注入不同 `subject_id`（脚本/测试替身）验证。这是后续 A1/A5 的已知阻塞点。

---

## 5. 数据基线（课程 / 课时 / 历史会议）

### 5.1 活跃库 `yuan_classroom_local`（head `f1a2b3c4d5e6`）

| 对象 | 标识 | 归属 | 可读性 |
|---|---|---|---|
| 课程1「待整理历史资料」（term=历史导入，回填产生） | `b630d44e-2f3f-44b7-b4da-e89a36133567` | `local-user` | 空课程 |
| 课程2「本机试用·牛顿第二定律」 | `f95f1e52-b222-42e2-a1a9-2456717f975a` | `local-user` | 试用样例 |
| 课时「牛顿第二定律短课」 | `b8a9597f-f704-419a-ad97-0833c6b4f8ed` → 课程2 | `local-user` | `status=processed`、`legacy_meeting=false`，已挂 active audio/transcript/note 指针 |

非空表行数：`index_blocks=13`、`parsed_blocks=10`、`artifact_evidence=6`、`evidence_spans=6`、`transcript_segments=5`、`job_checkpoints=4`、`processing_jobs=3`、`outbox_events=3`、`courses=2`、`index_intents=2`、`chunk_items=2`、`note_versions=2`、`chunk_manifests=2`、`provider_permit_slots=2`、`chat_sessions=1`、`source_documents=1`、`transcript_versions=1`、`audio_assets=1`、`meetings=1`、`index_versions=1`、`source_document_versions=1`。

- `transcripts`（旧表）**0 行**；转写落在新表 `transcript_segments`（5 段，说话人「说话人1..5」，内容为牛顿第二定律 demo，时间 0–45260ms）。
- `note_versions` 2 行：1 `superseded` + 1 `published`（已发布版本 `9109b159-…`）。

### 5.2 旧库 `yuan_meet`（head `d5e6f7a8b9c0`）

- `courses=1`（仅回填的「待整理历史资料」）、`meetings=0`、`transcripts=0`、`summaries=0`、`decisions=0`、`action_items=0`、`risks=0`。
- 唯一非空业务表 `chat_sessions=1`。

### 5.3 缺口（明确记录）

**无真实历史会议数据样本。** 两个库中均不存在带转写/纪要/决策的旧会议记录；`yuan_meet` 只有回填课程与 1 条会话。A7「旧库副本迁移，与迁移前按标识比对」当前只能用 `verify_olddata_migration_phase1.py` **自造的 mock/real/unknown 三类旧数据**验证，无法用真实旧记录核对。迁移验证结果见 §6。

---

## 6. 迁移验证结果（隔离副本）

迁移脚本自建并 `DROP/CREATE` 独立测试库，**不触碰活跃库**。

### 6.1 空库全链迁移（`verify_fresh_migration_phase1.py`）

修复前 **7/8 PASS，1 FAIL**（`[FAIL] 迁移到达 head (got f1a2b3c4d5e6)`，断言硬编码期望 `f7a8b9c0d1e2` 已过期；原始失败日志保留于证据 `fresh_migration_stdout.before-fix.txt`）。

修复后 **8/8 PASS，退出码 0**：

```
[PASS] 迁移到达 head
[PASS] 领域表全部就位
[PASS] courses/chat_sessions/knowledge_documents.owner_id NOT NULL
[PASS] transcript_segments.source_transcript_id 存在
[PASS] 空库回填创建历史课程 + 零会议
[PASS] 全部约束就位
------------------------------------------------------------
TOTAL 8  PASS 8  FAIL 0
```

「迁移到达 head」现从 Alembic 迁移目录动态读取预期 head（`ScriptDirectory.get_heads()`，当前为 `f1a2b3c4d5e6`），与 `alembic_version` 表实际版本比较；版本不一致时仍记 FAIL 并退出非 0。

### 6.2 旧数据副本迁移（`verify_olddata_migration_phase1.py`）

结果 **16/16 PASS**（R4/R5 + F4）：旧 meeting+transcripts（重复 seq、负时间、倒置时间、缺失音频、mock/real/unknown 三种来源）迁移后：归入历史课程并标记 legacy、重复序号确定性去重、秒转毫秒、负/倒置时间置空、mock/real 各设活动指针、未知来源隔离不设指针、`source_transcript_id` 保留旧片段 ID 映射。

### 6.3 活跃库 schema 校验（`check_classroom_schema.py`）

`Classroom database schema is current.`（exit 0）——活跃库已位于 head `f1a2b3c4d5e6`。

---

## 7. 实际运行的命令及结果

以下为**一段可直接从仓库根目录运行的 PowerShell 块**（已在仓库根目录实测，全部退出码 0）。它显式切换到 `backend` 工作目录、设置 `PYTHONPATH`/`PYTHONUTF8`、隔离两个临时库，并在活跃库只读检查前用 `Remove-Item Env:DATABASE_URL` 恢复正确连接（回退读取 `backend/.env`），避免旧数据测试残留的 `DATABASE_URL` 使 schema 检查指向测试库。数据库口令不写入脚本，改由执行者预先设置的环境变量 `PGPASSWORD` 读取；变量缺失时在创建任何测试库之前抛错停止。

```powershell
# 在仓库根目录 yuan-meeting-main 运行（需先 docker compose up -d 拉起 postgres/redis）
# 口令由执行者预先设置：$env:PGPASSWORD = '<口令>'（勿写入脚本或公开证据）
$root = (Get-Location).Path
$py   = Join-Path $root 'backend\.venv\Scripts\python.exe'
$bak  = Join-Path $root 'backend'
$PG_PASS = $env:PGPASSWORD
if ([string]::IsNullOrEmpty($PG_PASS)) {
  throw '缺少数据库口令：请先执行 $env:PGPASSWORD = "<口令>" 再运行本块（在创建任何测试库之前停止）。'
}

$env:PYTHONPATH = $bak
$env:PYTHONUTF8 = '1'

function Invoke-MigrationStep([string]$name, [string]$db, [string]$script) {
  Write-Host ''
  Write-Host "===== $name（目标库 $db）====="
  Set-Location $bak
  $env:DATABASE_URL = "postgresql+asyncpg://postgres:${PG_PASS}@localhost:5432/$db"
  & $py $script
  Write-Host "[$name] exit=$LASTEXITCODE"
  docker exec yuan_meet_postgres psql -U postgres -d $db -tAc "SELECT 'alembic_version='||version_num FROM alembic_version;"
}

Invoke-MigrationStep '空库全链迁移'   'yuan_meet_classroom_fresh_test'  'scripts\verify_fresh_migration_phase1.py'
Invoke-MigrationStep '旧数据副本迁移' 'yuan_meet_classroom_olddata_test' 'scripts\verify_olddata_migration_phase1.py'

Write-Host ''
Write-Host '===== 活跃库 schema 校验（yuan_classroom_local，恢复正确连接）====='
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue   # 回退读取 backend/.env
Set-Location $bak
& $py 'scripts\check_classroom_schema.py'
Write-Host "[活跃库 schema 校验] exit=$LASTEXITCODE"
docker exec yuan_meet_postgres psql -U postgres -d yuan_classroom_local -tAc "SELECT 'alembic_version='||version_num FROM alembic_version;"
```

实测记录（口令不落盘）：

| 步骤 | 目标库 | 迁移版本 | 结果 | 退出码 |
|---|---|---|---|---|
| 空库全链迁移 | `yuan_meet_classroom_fresh_test` | `f1a2b3c4d5e6` | `TOTAL 8  PASS 8  FAIL 0` | 0 |
| 旧数据副本迁移 | `yuan_meet_classroom_olddata_test` | `f1a2b3c4d5e6` | `TOTAL 16  PASS 16  FAIL 0` | 0 |
| 活跃库 schema 校验 | `yuan_classroom_local` | `f1a2b3c4d5e6` | `Classroom database schema is current.` | 0 |

数据基线快照用 `docker exec yuan_meet_postgres psql ...` 采集（见 §5 与证据文件）。

---

## 8. 对应验收编号

- **A1 / R1（归属）**：现状核查 —— 归属/权限统一走 `access.py`，但**多用户身份缺失**（见 §4.5），属后续阻塞项。
- **A7 / R7（入口与兼容）**：现状核查 —— 默认入口仍为会议列表页，旧上传入口 410，**课堂默认入口未切换**；旧数据迁移已验证（合成数据）。
- 迁移验证直接对应 `02-plan.md` 步骤 1 的「fresh 与 olddata 迁移检查」；R4/R5 回填规则在 olddata 脚本中 16/16 通过。
- 其余 A2–A6 属阶段 2–5，本阶段仅输出现状与改动清单。

---

## 9. 后续改动清单（阶段 2–5）

按优先级：

1. **【阶段 5 · 前端入口】默认入口仍是会议页，未切换课堂主页**。需新增课程列表/创建页 + 课时详情页，串联「上传→转写→整理→草稿确认→发布→问答」，并把 `routes.tsx` 默认路由切到课程列表。这是贯穿阶段 2–5 的主线。
2. **【阶段 4 · 问答】`chat_service.chat_stream` 仍走旧「文档+决策」RAG**，未接 `retrieval_service` 的课堂双路检索（转写/课件/笔记 + 结构化知识点/例题）。A5 的引用回跳、课程/课时范围、证据不足提示需在此处接线。
3. **【阶段 2 · 多用户】HTTP 无第二身份**，`get_scope()` 恒为 `local-user`。A1/A5 的跨用户隔离需以服务层注入 `subject_id` 的替身/脚本验证，或补一个仅测试用的身份注入机制。
4. **【阶段 3】整理/发布后端已存在**（`organize_service` + worker + note publish + index intent），前端缺整理触发、草稿确认、发布操作 UI。
5. **【阶段 2 · 输入】录音上传/转写/课件解析后端已存在**（`/lectures/{id}/audio`、`/courses/{id}/documents`），前端缺课时级上传与转写段点选；真实 ASR 与失败重试（A2）需在课堂入口验证。
6. **【阶段 5 · 兼容】旧会议入口 `POST /meetings/{id}/upload` 已 410**，前端会议详情页仍调用它，需一并重接到课堂上传或隐藏旧入口。
7. **【阶段 2/5 · 旧数据】无真实旧会议样本**，A7 迁移对照只能基于合成数据；如需真实对照，需先提供旧库样本（否则在验收中登记该缺口）。

---

## 10. 证据路径

| 证据 | 路径 |
|---|---|
| 空库迁移 stdout/stderr（修复后 8/8） | `docs/classroom-agent/reset-v1/evidence/phase1/fresh_migration_stdout.txt` / `_stderr.txt` |
| 空库迁移 原始失败日志（修复前 7/8） | `docs/classroom-agent/reset-v1/evidence/phase1/fresh_migration_stdout.before-fix.txt` / `_stderr.before-fix.txt` |
| 旧数据迁移 stdout/stderr | `docs/classroom-agent/reset-v1/evidence/phase1/olddata_migration_stdout.txt` / `_stderr.txt` |
| 数据基线快照（课程/课时/非空表/片段） | `docs/classroom-agent/reset-v1/evidence/phase1/data_baseline.txt` |
| 复验端到端运行记录（库名/版本/结果/退出码） | `docs/classroom-agent/reset-v1/evidence/phase1/rework_phase1_run_summary.txt` |

证据中未含任何密钥或个人资料（数据库口令为本地开发默认值，公开证据以 *** 代称，不出现在任何输出；样例内容为公开物理知识点 demo）。

---

## 11. 未验证项与阻塞原因

- **真实 ASR/LLM 未调用**：本阶段为数据/迁移核查，未触发付费转写或模型推理；A2/A3/A5 的真实供应商链路留待阶段 2–4 验证。
- **多用户跨用户隔离（A1/A5）未验**：HTTP 层无第二身份（§4.5）。
- **真实旧会议数据迁移（A7）未验**：无旧样本（§5.3）。
- **前端课堂主页/课时详情未验**：页面尚不存在（§4.1）。

---

## 12. 失败记录

- 复验前：`verify_fresh_migration_phase1.py` `[FAIL] 迁移到达 head (got f1a2b3c4d5e6)` —— 断言硬编码期望 `f7a8b9c0d1e2`（过期），非迁移失败；原始失败日志保留于 `fresh_migration_stdout.before-fix.txt`。
- 复验后：该断言已改为动态读取迁移目录 head（保留版本不一致时的失败能力），空库 8/8、旧数据 16/16、活跃库 current，全部退出码 0，无失败。

---

## 13. 真实 vs 模拟

- **真实**：PostgreSQL 16 + pgvector 迁移（alembic 全链）、数据库 schema 校验、活跃库数据读取，均为真实服务。
- **模拟/合成**：旧数据迁移用的旧 meeting/transcripts 为脚本自造样例（非真实历史数据）；迁移验证不涉及任何 LLM/ASR 调用。
