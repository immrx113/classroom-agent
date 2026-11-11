# 阶段 4 交接说明 · 问答/检索闭环（reset-v1 · A5 / R5）

日期：2026-11-11 ｜ 执行人：Claude（deepseek-v4-pro）｜ 范围：仅阶段 4（A5 / R5），停在阶段 4 等待**独立复验**。

> 本阶段在「课堂整理与发布（阶段 3）」已冻结的基础上，把课堂问答闭环接入：**问答入口先做课程/课时范围检索（两路召回 → RRF 融合 → 来源去重 → 重排 → 预算邻接上下文），再流式返回答案与可跳转来源标识**。对齐冻结清单 A5 / R5。

---

## 1. 本阶段完成内容（摘要）

将「课堂问答」改造为**范围检索 + 两路召回融合 + 流式答案 + 可跳转来源**，并对齐冻结清单 A5 / R5：

- **统一范围约束（R5）**：问答入口复用检索服务的权限/发布/课程-课时范围约束——课程范围可查该课全部课时，课时范围仅限本课时（`lecture_id IS NULL` 的课程共享块 + 本课时块），排除兄弟课时与其他课程的材料。范围预校验走 `retrieval_service._normalize_scope`，404/403 以 HTTP 语义提前拒绝，不落入 SSE 错误流。
- **两路召回 + 融合去重（R5）**：资料路（转写/课件/已发布笔记）与结构化路（知识点/例题）在 `retrieval_service.search` 内做向量 + 词法两路召回、RRF 融合、来源身份去重、规则重排、top_k、预算邻接上下文，产出统一证据（`trace` 记录 `vector/lexical/fused/deduped/selected` 各计数与两路是否命中）。
- **流式答案 + 来源标识（R5）**：`classroom_chat_stream` 依次产出 `sources`（来源 + trace）→ `token`（答案增量）→ `done`（状态）。有证据时走真实 LLM 流式（开启付费调用时）或确定性摘录（无 LLM / 测试）；证据不足（`no_evidence` / `retrieval_error`）时**仅返回确定性「依据不足」说明，不调用 LLM 编造**。
- **前端问答页（R5/A5）**：新增「课堂问答」页（`/classroom-qa`），课程/课时范围选择、轮次对话、SSE 流式渲染、来源卡片**点击跳转**（转写→音频回听定位 `start_ms`，课件→打开原文块按 `block_id`/页码定位），复用资料预览与音频定位。侧栏新增入口。

7 项自动化验证全部通过（`TOTAL 7 PASS 7 FAIL 0`），证据落盘于 `evidence/phase4/`。**点击引用截图（浏览器实测）4/4 通过**（A5-browser-1 笔记定位 / A5-browser-2 转写回听 / A5-browser-3 课件定位 / A5-browser-4 无脚本错误），证据落盘于 `evidence/phase4/browser/`。

---

## 2. 工作区 / 版本标识

- 仓库：`C:\Users\11322\Desktop\cs\project\yuan-meeting-main`（**非 Git 工作树**，无提交号；以 alembic 迁移版本 + 源码 SHA256 作为版本标识）。
- 后端 Alembic **head = `7d8e9f0a1b2c`**（本版**无新增迁移**）。
- 验证运行于**隔离测试库** `yuan_resetv1_p4_accf309831_main`（脚本创建、跑完即删），**未触碰活跃库 `yuan_classroom_local`**。
- 基础设施：`docker compose up -d`（`yuan_meet_postgres:5432` + `yuan_meet_redis:6379`），本阶段验证仅依赖 PostgreSQL。
- 后端运行 Python：`backend\.venv\Scripts\python.exe`（Python 3.12.10）。

---

## 3. 修改文件（本轮）

### 3.1 后端业务代码

- `backend/app/services/chat_service.py`（**本阶段核心改动**）：
  - 新增常量 `_CLASSROOM_SOURCE_LABEL`（转写/课件/笔记 → 中文标签）、`CLASSROOM_INSUFFICIENT_TEXT`（证据不足确定性说明）、`_classroom_answerer_fn`（测试注入替身，生产恒 `None`）。
  - 新增 `classroom_chat_stream(db, query, scope, top_k=None, embed=None)`：调用 `retrieval_service.search` → `sources` 事件（含 `trace`）→ 有证据时流式答案、无证据时仅返回「依据不足」说明 → `done`。答案路径优先级：`_classroom_answerer_fn`（测试）→ 真实 LLM 流式（`self.client` 非空且 `CLASSROOM_PAID_CALLS_ENABLED`）→ `_classroom_deterministic_answer`（确定性摘录，逐条标注来源编号）。
  - 新增 `_classroom_llm_stream`（真实 LLM：仅基于检索材料、引用来源编号、不足时明确说明）、`_classroom_deterministic_answer`、`_classroom_source_label`。
- `backend/app/api/chat.py`（新增端点）：
  - 新增 `ClassroomChatRequest`（`query` / `course_id` / `lecture_id`）。
  - 新增 `POST /chat/classroom/stream`：校验 query 非空、解析 UUID、构建 `chat_scope = ScopeContext(subject_id, course_id, lecture_id)`，先 `await retrieval_service._normalize_scope(db, chat_scope)` 预校验，再 SSE 流式序列化 `classroom_chat_stream` 事件（`json.dumps(..., ensure_ascii=False, default=str)`）。

### 3.2 前端代码

- `frontend/src/api/chat.ts`：新增 `ClassroomChatEvent` 接口与 `streamClassroomChat(params)` 异步生成器（POST `/chat/classroom/stream`，解析 SSE `data:` 行）。
- `frontend/src/features/retrieval/pages/classroom-qa-page.tsx`（**新增页面**）：课程/课时范围选择、轮次对话、SSE 流式渲染、`SourceCard` 点击跳转（转写→音频回听 `playAt`/`handleLoadedMetadata`；课件→`getDocumentBlocks` 打开原文块；笔记→`getNote(source_version_id)` 打开正文，`source_version_id` 即 `NoteVersion.id`，正文仅取 confirmed 条目与后端 `_note_text` 对齐）。复用资料预览与音频定位。
- `frontend/src/types/index.ts`：扩展 `NoteContent`（`knowledge_points` / `examples` / `key_points` 的 status/problem/derivation 等字段）。
- `frontend/src/router/routes.tsx`：懒加载 `ClassroomQaPage` + 路由 `classroom-qa`。
- `frontend/src/components/layout/sidebar.tsx`：新增 `Sparkles` 图标 + 导航项「课堂问答」(`/classroom-qa`)。

### 3.3 验证脚本（证据，新增）

- `backend/scripts/verify_resetv1_phase4.py`：隔离库 + 可控替身（确定性嵌入 + `_classroom_answerer_fn` 空 → 确定性摘录），覆盖 **7 项** A5。
- `backend/scripts/seed_phase4_browser.py`：写独立种子库 `yuan_resetv1_p4_browser`（真实可解码 WAV 8s 音频 + 已发布例题笔记 + 已解析课件页），确定性 embedding，无收费调用；落 `backend/runtime/phase4-browser-state.json`。
- `backend/scripts/serve_phase4_browser.py`：复用 `app.main:app` 连种子库，`OPENAI_API_KEY=""` + `INDEX_EMBEDDING_PROVIDER=deterministic`，起 `127.0.0.1:8787`。
- `backend/scripts/verify_phase4_browser.cjs`：Edge headless（`--autoplay-policy=no-user-gesture-required`）驱动 `/classroom-qa` 完成「提问 → 流式答案 → 点击来源跳转」并截图，覆盖 **4 项**浏览器实测（笔记定位 / 转写回听 / 课件定位 / 无脚本错误）。

---

## 4. 对应验收编号（冻结判据逐字保留）

| 编号 | 操作（逐字） | 冻结通过条件与证据（逐字） | 结果 |
|---|---|---|---|
| **A5 / R5** | 分别问课件细节、结构化例题、资料外问题和另一个课时的标记词；切换课时与课程范围。 | 保留两路召回与融合记录、流式响应和点击引用截图。引用对应原文；课时范围排除兄弟课时，课程范围可查该课其他课时，当前课程范围排除其他课程的标记词和材料，资料外问题提示依据不足。 | **通过（自动化 7/7 + 浏览器点击引用 4/4）**（见 §9 浏览器证据） |

> R5（`01-requirements.md`）逐字：`R5 问答|支持本课时或本课程提问。资料路检索转写、课件和已发布笔记；结构化路检索知识点及例题。融合后流式回答，引用能定位来源，证据不足时明确说明。`

---

## 5. 验证结果（`verify_resetv1_phase4.py`）

`TOTAL 7 PASS 7 FAIL 0`，退出码 0。逐项关键值（完整落盘于 `results.json`）：

| 检查项 | 结果 | 关键值 |
|---|---|---|
| A5a 课件细节（本课时） | ✅ | `query=DOCSTACK_PAGE1`、`status=ok`；SSE 事件 `sources/token/done`；`trace={vector:4, lexical:1, branches:[vector,lexical], fused:4, deduped:4, selected:4}`；top 命中 `source_type=document / page_no=1 / block_id=p1`，内容含标记 `DOCSTACK_PAGE1`；`streamed=true`、`two_path=true` |
| A5b 结构化例题（已发布笔记） | ✅ | `query=EXAMPLECALC_3PLUS2`；top `source_type=note`、`source_lecture_id` 指向本课时，内容含标记 + 例题 `计算 2+3` / `2+3=5`；答案逐条标注来源，引用对应原文 |
| A5c 资料路转写召回 | ✅ | `query=LINMARK_ALPHA`；top `source_type=transcript / start_ms=0 / end_ms=1000 / source_audio_asset_id`（带音频定位），内容含标记 |
| A5d 资料外问题 | ✅ | `query=龘靐齉`、`status=no_evidence`、`total=0`；SSE 仍产出 `sources/token/done`；答案 = 「依据不足：在当前课程/课时范围内未检索到…请补充相关材料，或换一种问法。」 |
| A5e 课时范围排除兄弟课时 | ✅ | L1 范围问 L2 专属标记 `L2SIBLING_BETA`：结果 3 条均属 L1（课件/转写/笔记），**不含 L2 标记** |
| A5f 课程范围可查该课其他课时 | ✅ | C1 范围问 `L2SIBLING_BETA`：命中 4 条，其中含 `source_lecture_id` 指向 L2 的转写块 |
| A5g 课程范围排除其他课程（含正向对照） | ✅ | C1 范围问 C2 专属标记 `C2OTHER_GAMMA`：`contains=false`、`total=5`；C2 范围问自身：`contains=true`、`total=1` |

---

## 6. 实际运行的命令及结果（复验可复跑）

命令涉及数据库口令（来自 `backend/.env` 的 `settings.DATABASE_URL`），脚本不打印、证据不落盘（`config.json`/`run.json` 仅存隔离库**名称**，不含连接串）。

```powershell
# backend 目录（脚本已自动把 backend/ 加入 sys.path，无需手工设 PYTHONPATH）
.\.venv\Scripts\python.exe scripts\verify_resetv1_phase4.py
# → TOTAL 7  PASS 7  FAIL 0（exit 0）
```

实测记录：

| 步骤 | 脚本 | 结果 | 退出码 |
|---|---|---|---|
| A5 七项自动化 | `verify_resetv1_phase4.py` | `TOTAL 7 PASS 7 FAIL 0` | 0 |

脚本自建隔离库 → `alembic upgrade head`（`7d8e9f0a1b2c`）→ 构造课程 C1（L1/L2 两课时）+ 课程 C2（各含专属转写/课件/笔记/例题标记）→ `build_index` + `activate` → 跑 7 项 → 落盘证据 → 删除隔离库。**不触碰活跃库**。

**从课堂入口发起的复跑路径**（脚本内部实现，验收方可直接复跑整个脚本）：
1. 构造 C1 课时 L1（转写 `LINMARK_ALPHA` + 课件 `DOCSTACK_PAGE1` + 已发布笔记 `EXAMPLECALC_3PLUS2`）、课时 L2（转写 `L2SIBLING_BETA`）、课程 C2（转写 `C2OTHER_GAMMA`）；
2. `build_index` + `activate` 建索引；
3. `POST /api/chat/classroom/stream`（HTTP 实际入口，`ASGITransport`）逐项提问，解析 SSE 的 `sources/token/done`，核对 `status` / `trace` / top 来源与范围边界。

---

## 7. 证据路径

| 证据 | 路径 |
|---|---|
| 七项结果（JSON，含逐项 detail） | `docs/classroom-agent/reset-v1/evidence/phase4/results.json` |
| 配置摘要（检索常量/嵌入提供方/隔离库名/标记词，无口令） | `docs/classroom-agent/reset-v1/evidence/phase4/config.json` |
| 运行记录（起止时间/退出码/隔离库名） | `docs/classroom-agent/reset-v1/evidence/phase4/run.json` |
| 源码 SHA256 快照（版本标识） | `docs/classroom-agent/reset-v1/evidence/phase4/source-manifest.json` |
| 浏览器实测结果（JSON，4 项 + 请求轨迹） | `docs/classroom-agent/reset-v1/evidence/phase4/browser/results.json` |
| 浏览器截图（笔记定位 / 转写回听 / 课件定位） | `docs/classroom-agent/reset-v1/evidence/phase4/browser/item1-note.png`、`item2-transcript.png`、`item3-document.png` |

证据中**未含任何密钥或口令**（数据库口令不出现；样例内容为公开数据结构知识点 demo，无个人信息）。

---

## 8. 真实 vs 模拟

- **真实**：PostgreSQL 16 真实写入（隔离库）；`alembic upgrade head` 真实迁移；`build_index` + `activate` 真实建索引；HTTP 请求经 `ASGITransport` 进程内真实发到 `POST /chat/classroom/stream`，SSE 逐条事件真实流式产出；范围权限/发布约束（`retrieval_service.search` + `_normalize_scope`）为真实业务代码；`retrieval_service.search` 的两路召回/RRF 融合/去重/重排/邻接上下文为真实代码路径。
- **模拟**：**嵌入为确定性嵌入**（`DeterministicEmbedding`，`INDEX_EMBEDDING_PROVIDER=deterministic`，无真实供应商调用）；**答案路径为确定性摘录**（`_classroom_answerer_fn=None` 且 `OPENAI_API_KEY=""` → `self.client=None` → 不走真实 LLM），逐条标注来源编号便于核对「引用对应原文」。语义质量属 P1 人工项，`CLASSROOM_PAID_CALLS_ENABLED` 默认值记录为 `true`（配置摘要中如实记录，实际本验证未触发真实付费调用）。

**浏览器实测（A5 点击引用）的真实 vs 模拟**：前端为**真实 Vite dev server + 真实 React 页面**（无请求拦截/夹具），后端为**真实 `app.main:app`**（独立种子库 `yuan_resetv1_p4_browser`），Playwright 走**真实 Edge headless** 完成「提问 → SSE 流式答案 → 点击来源跳转 → 音频真实播放 / 课件真实取块 / 笔记真实 `getNote`」。模拟仅限：确定性 embedding + 确定性摘录答案（不触发付费 LLM），音频为脚本生成的可解码 8s 正弦 WAV（真实 `AudioAsset` 服务路径回放，非固定时间伪造）。

---

## 9. 未验证项

> **点击引用截图（浏览器证据，A5 冻结项）——已完成，4/4 通过。** 冻结清单要求「点击引用截图」，现已由 `verify_phase4_browser.cjs` 在真实前端 Vite + 真实后端（独立种子库）+ Edge headless 下完成「提问 → 流式答案 → 点击来源跳转」实测：A5-browser-1 笔记定位（点击「打开笔记」，打开正文与回答引用一致，例题 `EXAMPLECALC_3PLUS2` 可核对）、A5-browser-2 转写回听（点击「回听来源」，音频定位 `3.115s` 且 `paused=false`，对应原句 `线性表定义讲解 LINMARK_ALPHA …`）、A5-browser-3 课件定位（点击「打开原文」，显示「第 1 页」+ 原文块 `栈的抽象数据类型 DOCSTACK_PAGE1 …`）、A5-browser-4 无页面脚本错误。证据落盘 `evidence/phase4/browser/`（`results.json` + `item1-note.png`/`item2-transcript.png`/`item3-document.png`，同一份 `results.json` 记录作业/课程/课时/来源标识）。

- **真实 LLM 语义质量（P1 人工项）**：答案是否语义准确未用真实模型核对，本阶段走确定性摘录路径，属 P1。
- **结构化路（知识点/例题）的独立召回验证**：本阶段例题经「已发布笔记」来源召回（`note` 来源，A5b）；知识点/例题作为独立结构化索引块的两路召回边界沿用 `retrieval_service.search` 既有路径（阶段 3 已验），未在本阶段单独注入「知识点来源块」用例。
- A1 / A2 / A3 / A4 / A6 / A7 不在本阶段范围。

---

## 10. 失败记录（编写脚本时发现并修复）

- **资料外问题被判 `ok` 而非 `no_evidence`（脚本用例设计，首次 6/7）**：确定性嵌入对字符 n-gram 共享，任何 ASCII 标记词（如 `NOMATERIAL_DELTA`）与全部内容块仍有非零余弦相似度，向量路给出模糊命中 → 未触发 `no_evidence`。修复：把资料外标记词改为罕见 CJK 字符 `龘靐齉`（与全部内容块零共享 n-gram → 余弦 0 → 低于 `INDEX_RETRIEVAL_RELEVANCE_THRESHOLD=0.1`）→ `no_evidence`。重跑 7/7 通过。
- **脚本退出时 SQLAlchemy 连接池 GC 告警（无害）**：`drop_db` 后连接池清理产生 SAWarning/Greenlet 噪声，仅出现在退出阶段，结果本身干净；复跑时以 `2>$null` 抑制无关告警。

---

## 11. 待办（阶段 4 独立复验通过前不进入阶段 5）

- 停在本阶段等待独立复验。**后端七项回归 + 浏览器点击引用均已通过**：验收方按 §6 命令复跑 `verify_resetv1_phase4.py` 即可得到 `7/7`；浏览器证据复跑见 §9（`verify_phase4_browser.cjs`，`4/4`），截图与结果已落盘 `evidence/phase4/browser/`。二者区分：后端 `7/7` 覆盖「范围约束 / 两路召回融合 / 流式事件 / 引用对应原文字段」的自动化断言；浏览器 `4/4` 覆盖「真实页面点击跳转」的实际结果（笔记打开正文、转写音频回听定位、课件页码/原文块、无脚本错误）。
- 如需人工抽查：范围边界（兄弟课时排除 / 课程范围含其他课时 / 其他课程排除）见 A5e/A5f/A5g；资料外「依据不足」见 A5d；引用对应原文（课件页/例题/转写音频定位）见 A5a/A5b/A5c；点击引用跳转的实际页面行为见浏览器 `item1/2/3` 截图与 `results.json`。
