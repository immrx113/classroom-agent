# 阶段 5 交接说明 · 入口切换与整体回归（reset-v1 · A7 + A1–A7 复验）

日期：2026-11-11 ｜ 执行人：Claude（deepseek-v4-pro）｜ 范围：阶段 5（A7）+ 在最终代码版本上复验 A1–A7，**停在阶段 5 等待最终独立验收**。

> 本阶段完成两件事：**(1) A7 入口切换**——默认首页从「旧会议」切到「课程列表」，旧会议降为侧栏兼容入口；课时详情页补全「全课整理 → 草稿确认 → 发布」页面闭环。**(2) 在最终代码版本上把 A1–A7 的既有回归脚本全部重跑一遍**，并新增 A7 的浏览器全流程实测（默认首页走完上传→整理→草稿→发布→问答→引用回跳）与旧数据迁移前后对照。

> **最终独立验收结论（2026-11-11）**：整体验收**阻塞**，工程回归**全部通过**。A2 真实链路证据已按「复用 + 重新执行」补齐（新增 `verify_phase5_realasr_seek.cjs` + `evidence/phase5/realasr/`）；A7 因**无真实旧会议数据副本**保持阻塞，等待用户作出适用范围决定。真实 LLM / 真实嵌入未验证仅作说明记录，非放行条件。详见 [`16-phase5-final-acceptance.md`](16-phase5-final-acceptance.md)。

---

## 1. 本阶段完成内容（摘要）

- **入口切换（A7）**：`frontend/src/router/routes.tsx` 的 `index: true` 从 `MeetingListPage` 改为 `CourseListPage`；旧会议路由保留为 `meetings` / `meetings/:id`。`frontend/src/components/layout/sidebar.tsx` 重排导航：课程管理/课堂问答/课堂检索/课堂摘要/课堂讨论/学习对话/处理记录在前，「旧会议（兼容）」置底。
- **课时页补全整理/发布闭环（A7）**：`frontend/src/features/classroom/pages/lecture-detail-page.tsx` 新增「全课整理与发布」卡片（整理本课 → 草稿状态预览 → 确认发布 → 已发布），配套 `frontend/src/features/classroom/api.ts` 新增 `organizeLecture` / `listNotes` / `publishNote`。
- **A7 浏览器全流程实测**：真实本地 Vite + 真实本地后端（独立种子库），从默认首页真实创建课程/课时、上传录音+课件、转写、整理、草稿确认、发布、问答、引用回跳，9/9 通过（§5）。
- **A7 旧数据迁移前后对照**：空库全链迁移 8/8、旧数据副本全链迁移 16/16（含重复 seq_index / 负时间 / mock / real / 未知来源 / 缺失音频 / 旧片段 ID 映射 / 无隐式归课），证据落盘（§7）。
- **A1–A7 复验**：在最终代码版本（alembic head `7d8e9f0a1b2c`，无新增迁移）上重跑 A1–A6 全部回归脚本 + A7 浏览器，**全部通过**（§5）。

---

## 2. 工作区 / 版本标识

- 仓库：`C:\Users\11322\Desktop\cs\project\yuan-meeting-main`（**非 Git 工作树**，无提交号；以 alembic 迁移版本 + 源码 SHA256 作为版本标识）。
- 后端 Alembic **head = `7d8e9f0a1b2c`**（本阶段**无新增迁移**）。
- 基础设施：`docker compose up -d`（`yuan_meet_postgres:5432` + `yuan_meet_redis:6379`），本阶段验证仅依赖 PostgreSQL。
- 后端运行 Python：`backend\.venv\Scripts\python.exe`（Python 3.12.10）。
- 隔离库（不触碰活跃库 `yuan_classroom_local`）：浏览器证据 `yuan_resetv1_p5_browser`（种子脚本自建）；各回归脚本自建随机前缀隔离库（跑完即删）；迁移脚本自建 `yuan_meet_classroom_fresh_test` / `yuan_meet_classroom_olddata_test`。

---

## 3. 修改文件（本轮）

### 3.1 前端（入口切换 + 整理/发布 UI）

- `frontend/src/router/routes.tsx`：`index: true` → `<CourseListPage />`；新增 `meetings` / `meetings/:id` 旧会议兼容路由（`courses` / `courses/:courseId` / `courses/:courseId/lectures/:lectureId` 已存在）。
- `frontend/src/components/layout/sidebar.tsx`：`navItems` 重排，`/meetings` 文案改为「旧会议（兼容）」置底。
- `frontend/src/features/classroom/api.ts`：新增 `organizeLecture(lectureId, transcriptVersionId)`、`listNotes(lectureId)`、`publishNote(noteId)`。
- `frontend/src/features/classroom/pages/lecture-detail-page.tsx`：新增「全课整理与发布」卡片（`organize-button` → 草稿 `note-status`/`note-preview` → `publish-button` → 已发布），`noteStatusLabel` / `notePreview` / `pickActiveNote` 辅助函数；整理/发布后轮询任务并刷新笔记列表。
- 前端构建：`npx tsc -b` 与 `npm run build` 均通过（exit 0）。

### 3.2 验证脚本（证据，新增）

- `backend/scripts/seed_phase5_browser.py`：建独立库 `yuan_resetv1_p5_browser`（alembic head）+ 生成可上传样例（8s 可解码 WAV + 带标记 Markdown 课件）；**不预置课程/课时/转写/笔记**（由浏览器真实创建）；落 `backend/runtime/phase5-browser-state.json`。
- `backend/scripts/serve_phase5_browser.py`：复用真实 `app.main:app` 连种子库，`OPENAI_API_KEY=""` + `DASHSCOPE_API_KEY=""` + `INDEX_EMBEDDING_PROVIDER=deterministic`；`FakeASRProvider`（可定制标记片段）+ `FakeOrganizeModel` + 后台派发循环（ASR/organize/index，1s/轮，模拟生产 worker 至少一次投递）；起 `127.0.0.1:8787`。
- `backend/scripts/verify_phase5_browser.cjs`：Edge headless 驱动，从 `http://localhost:5173/` 走完整链路并截图，覆盖 9 项（入口切换/建课/建课时/上传转写/上传课件/整理草稿/发布/问答引用回跳/无脚本错误）。

---

## 4. 对应验收编号（冻结判据逐字保留）

| 编号 | 操作（逐字） | 冻结通过条件与证据（逐字） | 结果 |
|---|---|---|---|
| **A7 / R7** | 对旧库副本执行迁移，与迁移前按标识比对；从默认首页走完上传至引用回跳。 | 旧记录数量、内容和归属可核对，无隐式归课；用户全程可经课堂页面完成任务。保存迁移对照与浏览器操作记录。 | **阻塞**（迁移逻辑/浏览器侧通过：迁移对照 16/16 + 空库 8/8 + 浏览器全流程 9/9；但「旧库副本」为**构造样本**，缺**实际旧会议数据副本**，见 §9 与 `16-phase5-final-acceptance.md` §4） |

> A1–A6 复验（最终代码版本重跑，逐字判据见各自阶段交接文档）汇总：

| 编号 | 脚本 | 结果 |
|---|---|---|
| A1a / A2（课程课时归属 + 资料输入） | `verify_phase2_classroom_input.py` | **通过 2/2** |
| A3 / A4 / A6（整理/发布/预算/超时） | `verify_resetv1_phase3.py` | **通过 20/20** |
| A5（问答/检索闭环） | `verify_resetv1_phase4.py` | **通过 7/7** |
| A1 基线（空库迁移） | `verify_fresh_migration_phase1.py` | **通过 8/8** |
| A7 迁移对照 | `verify_olddata_migration_phase1.py` | **通过 16/16** |
| A7 浏览器全流程 | `verify_phase5_browser.cjs` | **通过 9/9** |

> v1.1 版本变更：**A1b 真实双用户会话不适用**（产品决定采用个人单用户模式），A1a 测试身份归属回归继续执行（由 `verify_phase2_classroom_input.py` 的 A1 覆盖：跨用户直接读写被拒 404 且内容未改变）。

---

## 5. 验证结果

### 5.1 A7 浏览器全流程（`verify_phase5_browser.cjs`，9/9，退出码 0）

| 检查项 | 结果 | 关键值 |
|---|---|---|
| A7-browser-0 入口切换 | ✅ | `http://localhost:5173/` 默认首页标题为「课程管理」（heading 命中），非旧会议列表 |
| A7-browser-1 新建课程 | ✅ | 页面创建「阶段五验收课程」→ 进入 `/courses/{id}`（36 位 UUID） |
| A7-browser-2 新建课时 | ✅ | 页面创建「第 1 讲 加法」→ 进入 `/courses/{cid}/lectures/{id}` |
| A7-browser-3 上传录音并转写 | ✅ | 4 段转写，含例题标记 `P5EXAMPLE_CALC_2PLUS3`（片段有序、含时间区间） |
| A7-browser-4 上传课件 | ✅ | 课件条目 `phase5-trial` `parsed · 3 块` 可见 |
| A7-browser-5 整理产出草稿 | ✅ | `note-status=草稿（待确认）`；预览含例题 `P5EXAMPLE_CALC_2PLUS3 计算 2+3` + 解 `2+3=5`，未发布 |
| A7-browser-6 草稿确认并发布 | ✅ | `note-status=已发布` |
| A7-browser-7 问答 + 引用回跳 | ✅ | 提问 `P5EXAMPLE_CALC_2PLUS3`，答案引用「笔记」来源；点击「打开笔记」打开正文含标记与推导，与回答引用一致 |
| A7-browser-8 无页面脚本错误 | ✅ | `errors=0` |

> 浏览器问答返回（确定性摘录）为 `[1] 来源：转写｜…【例题】P5EXAMPLE_CALC_2PLUS3 … [2] 来源：笔记｜第 1 讲 加法 … 2+3=5 …`；打开笔记正文与回答引用逐字一致。

### 5.2 A7 旧数据迁移对照（`verify_olddata_migration_phase1.py`，16/16）

从旧库状态 `293138585702`（无课程领域表）塞入带「重复 seq_index / 负时间 / mock / real / 未知来源 / 缺失音频」的旧 meeting+transcripts，一路 `alembic upgrade head` 补偿回填：历史课程已创建、旧会议全部归入历史课程并标记 legacy（**无隐式归课**）、mock/real 分类与活动指针、重复序号去重（保留最早 + 秒转毫秒）、负时间置空、旧片段 ID 映射保留、未知来源隔离、缺失音频显式 NULL 状态，`TOTAL 16 PASS 16 FAIL 0`。

### 5.3 A1–A6 复验（最终代码版本重跑，全部退出码 0）

- `verify_fresh_migration_phase1.py` → `TOTAL 8 PASS 8 FAIL 0`（alembic head 动态读取，到达 `7d8e9f0a1b2c`）。
- `verify_olddata_migration_phase1.py` → `TOTAL 16 PASS 16 FAIL 0`。
- `verify_phase2_classroom_input.py` → `TOTAL 2 PASS 2 FAIL 0`（A1 归属/跨用户拒绝 404 + 内容不变；A2 真实录音/课件上传 → 有序转写段 3 段 → 课件 10 块定位 → 模拟失败 retry_wait → 重试 succeeded）。
- `verify_resetv1_phase3.py` → `TOTAL 20 PASS 20 FAIL 0`（A3a–A3e 五类整理 + A4a–A4k 十一类失效引用/发布边界 + A6a–A6d 四类预算/超时/运行详情）。
- `verify_resetv1_phase4.py` → `TOTAL 7 PASS 7 FAIL 0`（A5a–A5g：课件细节/结构化例题/转写定位/资料外 no_evidence/课时排除兄弟/课程范围/其他课程排除）。

---

## 6. 实际运行的命令及结果（复验可复跑）

命令涉及数据库口令（来自 `backend/.env` 的 `settings.DATABASE_URL`），脚本不打印、证据不落盘口令。浏览器实测需先起后端 + 前端：

```powershell
# —— 后端（backend 目录）——
.\.venv\Scripts\python.exe scripts\seed_phase5_browser.py     # 建隔离库 + 生成样例
.\.venv\Scripts\python.exe scripts\serve_phase5_browser.py    # 127.0.0.1:8787（后台派发循环）
# —— 前端（frontend 目录）——
npm run dev                                                   # 127.0.0.1:5173，/api 代理到 8787
# —— 浏览器实测（backend 目录，Node）——
node scripts\verify_phase5_browser.cjs                        # → 9/9

# —— A1–A7 回归（backend 目录，均退出码 0）——
$env:PYTHONPATH="."; $env:PYTHONUTF8="1"
.\.venv\Scripts\python.exe scripts\verify_phase2_classroom_input.py   # A1/A2  2/2
.\.venv\Scripts\python.exe scripts\verify_resetv1_phase3.py           # A3/A4/A6 20/20
.\.venv\Scripts\python.exe scripts\verify_resetv1_phase4.py           # A5 7/7
# 迁移（需将 DATABASE_URL 库名替换为 *_test，脚本内部 REFUSE 非 test 库）
.\.venv\Scripts\python.exe scripts\verify_fresh_migration_phase1.py   # 8/8
.\.venv\Scripts\python.exe scripts\verify_olddata_migration_phase1.py # 16/16
```

实测记录：

| 步骤 | 脚本 | 结果 | 退出码 |
|---|---|---|---|
| A7 浏览器全流程 | `verify_phase5_browser.cjs` | 9 项通过 | 0 |
| A7 旧数据迁移对照 | `verify_olddata_migration_phase1.py` | `16/16` | 0 |
| A1 空库迁移基线 | `verify_fresh_migration_phase1.py` | `8/8` | 0 |
| A1a/A2 资料输入 | `verify_phase2_classroom_input.py` | `2/2` | 0 |
| A3/A4/A6 整理发布 | `verify_resetv1_phase3.py` | `20/20` | 0 |
| A5 问答检索 | `verify_resetv1_phase4.py` | `7/7` | 0 |

---

## 7. 证据路径

| 证据 | 路径 |
|---|---|
| A7 浏览器结果（JSON，9 项 + 请求轨迹 + 标记词） | `docs/classroom-agent/reset-v1/evidence/phase5/browser/results.json` |
| A7 浏览器截图（首页/上传/整理/发布/问答） | `docs/classroom-agent/reset-v1/evidence/phase5/browser/item0-home.png`、`item1-upload.png`、`item2-organize.png`、`item3-publish.png`、`item4-qa.png` |
| A7 空库迁移 stdout | `docs/classroom-agent/reset-v1/evidence/phase5/fresh_migration_stdout.txt` |
| A7 旧数据迁移 stdout（含回填报告） | `docs/classroom-agent/reset-v1/evidence/phase5/olddata_migration_stdout.txt` |
| A1/A2 结果（JSON） | `docs/classroom-agent/reset-v1/evidence/phase2/results.json` |
| A2 真实 DashScope ASR 结果（JSON，5 段） | `docs/classroom-agent/reset-v1/evidence/phase2/real_results.json` |
| A2 浏览器证据（课件归属/点选 seek/课件定位/失败重试） | `docs/classroom-agent/reset-v1/evidence/phase2/browser/{results.json, item1-lecture1.png, item1-lecture2.png, item2-seek.png, item3-doc.png, item3-retry.png}` |
| **A2 真实 ASR 转写段点击（最终页面，本轮新增）** | `docs/classroom-agent/reset-v1/evidence/phase5/realasr/{results.json, item-seek.png}` |
| A3/A4/A6 结果（JSON） | `docs/classroom-agent/reset-v1/evidence/phase3/results.json` |
| A5 结果（JSON） | `docs/classroom-agent/reset-v1/evidence/phase4/results.json`（及 `browser/results.json`） |

证据中**未含任何密钥或口令**（数据库口令不出现；样例内容为公开数据结构/加法运算 demo，无个人信息）。

---

## 8. 真实 vs 模拟

- **真实**：PostgreSQL 16 真实写入（隔离库）；`alembic upgrade head` 真实迁移；浏览器实测前端为**真实 Vite dev server + 真实 React 页面**（无请求拦截/夹具），后端为**真实 `app.main:app`**（独立种子库 `yuan_resetv1_p5_browser`），Playwright 走**真实 Edge headless**；课程/课时由页面真实创建；上传→转写→整理→发布→问答→引用回跳全部经真实 HTTP 接口与真实业务代码路径（上传/转写作业派发、`organize` 整理入口、`publish` 发布 + `IndexIntent` 异步索引、`chat/classroom/stream` 问答 + 检索）；发布后索引构建与激活由后台派发循环真实完成。
- **模拟**：转写为 `FakeASRProvider`（注入 4 段带 `【例题】/【知识点】/【难点】` 标记的片段，不调真实供应商）；整理为 `FakeOrganizeModel`（本地解析标记，不调真实 LLM）；嵌入为 `DeterministicEmbedding(1024)`（`INDEX_EMBEDDING_PROVIDER=deterministic`）；问答答案为**确定性摘录**（`OPENAI_API_KEY=""` → `self.client=None` → `_classroom_deterministic_answer`，逐条标注来源编号，不触发付费 LLM）。音频为脚本生成的 8s 正弦 WAV（真实 `AudioAsset` 服务路径回放，非真实课堂录音）。**A1–A6 回归脚本**同理：隔离库 + ASGITransport 进程内 HTTP + FakeASRProvider / FakeOrganizeModel / 确定性嵌入 / 确定性摘录，均不调收费模型。

---

## 9. 未验证项 / 阻塞 / 仍缺失样本

> 本节区分「冻结清单阻塞项」与「说明记录（非放行条件）」，前者决定整体验收放行，后者仅记录不追加为放行条件。完整结论见 `16-phase5-final-acceptance.md`。

### 9.1 冻结清单阻塞项

- **A7 无实际旧会议数据副本（阻塞，唯一未闭合阻塞项）**：迁移逻辑本身已通过（`verify_olddata_migration_phase1.py` 16/16），但用的是**构造的旧库状态**（3 条合成 meeting：重复 seq_index / 负时间 / mock / real / 未知来源 / 缺失音频 / 倒置时间），验证的是迁移与归属规则，**不是真实历史记录**。经穷尽排查（`yuan_meet` 全表 0 行、无 dump/backup 文件、活跃库无迁移前历史会议），**无真实历史会议数据可作副本**。按验收方要求保持**阻塞**，等待用户作出适用范围决定（提供真实旧库副本，或明确「无历史数据、以构造样本为准」）。

### 9.2 说明记录（非放行条件）

- **真实整理 LLM / 真实问答 LLM 未验证**：A3/A4/A5/A7 的整理与问答在自动化与浏览器回归中均由 `FakeOrganizeModel` / 确定性摘录取证，未接通真实整理/问答 LLM。属语义质量（P1 人工项），不构成本次放行条件。
- **真实嵌入未验证**：索引用 `DeterministicEmbedding(1024)`（`INDEX_EMBEDDING_PROVIDER=deterministic`），未接通真实 `text-embedding-v3`。不构成本次放行条件。
- **真实课堂录音/课件非用户真实数据**：`newton-trial.wav` / `newton-trial.md` 由本机生成（真实音频、真实 DashScope 转写，但**不是用户的真实课堂数据**），用于验证真实链路而非真实业务内容。属说明记录。
- **A1b 真实双用户会话**：v1.1 明确不适用（个人单用户版），不做。
- 浏览器实测未覆盖「整理失败重试 / 预算不足 / 供应商超时」的页面交互（这些由 A3/A4/A6 后端回归覆盖，页面侧仅走成功路径）。

### 9.3 已补齐（A2 真实链路，非阻塞）

- **真实 ASR 已接通并复用**：阶段 2 `verify_phase2_real_http.py` 对真实录音 `newton-trial.wav` 发起真实 DashScope ASR 并成功（`evidence/phase2/real_results.json` 5 段，`provider=real`、`provider_task_id` 持久化，DB `transcript_versions`/`transcript_segments` 与之一致）。本轮**复用**该真实结果，未重新计费。
- **转写段点击定位已在最终页面版本重新执行**：新增 `verify_phase5_realasr_seek.cjs`（真实后端连 `yuan_classroom_local` + 真实 Vite 前端），点选真实转写段 seek 命中 0s/13.61s/23.2s，页面原句与真实 ASR 逐段一致，证据 `evidence/phase5/realasr/{results.json, item-seek.png}`（3/3）。

---

## 10. 失败记录 / 已知非阻塞观察

- **（非阻塞，既有）笔记正文单条结论双显**：笔记结构里 `key_conclusions` 与 `sections[].points` 为同一份结论列表（`organize_service.py` 中 `key_conclusions` 又作为 `sections` 的 `points`），故前端「草稿预览 / 打开笔记正文」会把同一条正文结论在「要点」下显示两次。此为阶段 3 既有结构 + 阶段 4 `noteContentText` / 阶段 5 `notePreview` 一致的展示层冗余，**不影响来源对应核对、不产生编造、不影响检索与验收**；如需消除可让预览只渲染 `sections` 或只渲染 `key_conclusions`（需同步 `classroom-qa-page.tsx` 的 `noteContentText` 与 `lecture-detail-page.tsx` 的 `notePreview`，并回归阶段 4/5 断言）。
- **回归脚本退出时 SQLAlchemy 连接池 GC 告警（无害）**：`verify_resetv1_phase4.py` 等退出阶段产生 SAWarning/Greenlet 噪声（`greenlet is being finalized`），仅出现在退出清理阶段，结果本身干净（`TOTAL N PASS N FAIL 0`），不影响判定。

---

## 11. 待办（整体验收阻塞解除前不进入下一阶段）

- **整体验收保持阻塞**，停在阶段 5 等待最终复验。工程回归全部通过（§5），阻塞点仅剩 A7 实际旧数据副本（§9.1），A2 真实链路证据已补齐（§9.3）。
- 解除 A7 阻塞需用户作出适用范围决定：① 提供真实旧会议数据库副本（含旧 `meetings` + `transcripts`），按旧记录标识（id）在隔离库做迁移前后数量/内容/归属比对；或 ② 明确「本项目无历史会议数据，A7 迁移对照以构造边界样本为准」。
- 全部回归命令见 §6；本轮新增 `node scripts/verify_phase5_realasr_seek.cjs`（A2 真实 ASR 转写段点击，最终页面版本），复跑需先起真实后端（`uvicorn app.main:app` 连 `yuan_classroom_local`）+ 真实前端（`npm run dev`）。
- 真实 LLM / 真实嵌入未验证为说明记录（§9.2），不追加为放行条件，不阻塞本阶段验收结论。

2026-11-11 验收更新：用户确认无历史会议数据，现采用 [v1.2 冻结清单](03-acceptance-v1.2.md)。[最终独立验收](17-final-review-v1.2.md)已通过；本文此前的阻塞结论作为历史记录。
