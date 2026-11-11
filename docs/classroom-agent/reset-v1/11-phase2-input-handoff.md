# 阶段 2 交接说明 · 课程、课时与资料输入（reset-v1）— 复验返工版

日期：2026-11-10 ｜ 执行人：Claude（deepseek-v4-pro）｜ 范围：仅阶段 2，停在阶段 2 等待**独立复验**。

本版为独立验收返工后的修订版，按复验意见 4 项逐条修复并补浏览器实测证据。原文（初版）见 git/文件历史；本文档为当前唯一有效交接。

---

## 1. 复验意见与本轮处理结果

独立验收提出 4 项返工（逐字）：

1. **修复课时课件归属**：首次加载、上传后刷新、切换课时都应遵守课时绑定；同一课程下两个课时分别上传带不同标记的课件，刷新两个页面证明各自只能看到所属课件；若支持课程共享资料，单独验证展示规则。
   → **已修复 + 已通过浏览器实测**（见 §5.1 检查项 1）。

2. **修复首次点选转写段**：播放器完成加载后刷新并首次点击第三段，断言从 13.610s 附近开始播放；再点其他段验证连续跳转；保存实测值与截图。
   → **已修复 + 已通过浏览器实测**（见 §5.1 检查项 2；实测 `third_s=13.61`、`first_s=0`、`fourth_s=23.2`）。

3. **补齐 A2 页面证据**：从页面上传录音和带已知页码、已知标记文字的课件，验证可定位原页；模拟一次转写失败，从页面查看原因、刷新并重试；保存作业标识、前后状态及有效输出版本数量。
   → **已补齐 + 已通过浏览器实测**（见 §5.1 检查项 3a / 3bcd；作业 `job_id`、前后状态、`output_version` 均落盘）。

4. **A1 分开报告**「身份依赖替换下的 HTTP 请求」与「实际双用户会话」；仍固定为 `local-user` 则登记为阻塞，不得把 A1 汇总为通过。
   → **已拆分**：A1a（身份依赖替换下的 HTTP 请求）**通过**；A1b（实际双用户会话）**阻塞**，A1 不汇总为通过（见 §4）。

---

## 2. 工作区 / 版本标识

- 仓库：`C:\Users\11322\Desktop\cs\project\yuan-meeting-main`（**非 Git 工作树**，无提交号；以 alembic 迁移版本作为版本标识）。
- 后端 Alembic **head = `f1a2b3c4d5e6`**；活跃库 `yuan_classroom_local`。
- 活跃配置 `backend/.env`：`ASR_MODE=real`、`CLASSROOM_PAID_CALLS_ENABLED=true`、OSS + DashScope 已配置（`get_provider().name` 实测为真实供应商）。
- 基础设施：`docker compose up -d`（`yuan_meet_postgres:5432` + `yuan_meet_redis:6379`）。
- 前端：React 19 + Vite；`npx tsc -b` 与 `npm run build` 均通过。
- 浏览器自动化：本机已有 Playwright（`C:/Users/11322/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright`）+ Edge（`C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe`）+ Node v24.14.0（`node` 在 PATH）。

---

## 3. 修改文件（本轮返工）

### 3.1 前端

- `frontend/src/features/classroom/pages/lecture-detail-page.tsx`
  - **课时课件归属**：移除独立 `mediaSrc` state；课件列表用 `scopedDocs = docs.filter(doc => doc.lecture_id == null || doc.lecture_id === lectureId)`，只展示「本课时绑定」+「课程共享（lecture_id 为空）」课件；卡片带 `data-testid="course-doc"` / `data-lecture-id` / `data-shared` 与徽标「本课时 / 课程共享」。
  - **首次点选转写段**：`playAt(startMs)` 改为在 `audio.readyState >= 1` 时直接 `audio.currentTime = startMs/1000; audio.play().catch(()=>{})`，否则暂存到 `pendingSeekMsRef` 待 `loadedmetadata` 应用（修复首次点选无响应）。
  - **失败后刷新恢复**：`loadAll` 额外拉取 `getLatestJob(lectureId)`，刷新后能恢复最新 ASR 任务（含失败原因 + 重试按钮）；新增 `errorLabel()` 中文错误映射；任务徽标带 `data-testid="job-status"`、错误 `data-testid="job-error"`、重试 `data-testid="job-retry"`。
  - **`audio.play()` 拒绝处理**：`playAt` / `handleLoadedMetadata` 的 `void audio.play()` 改为 `audio.play().catch(()=>{})`，消除快速连续点选时的未处理 Promise 拒绝（浏览器报 `The play() request was interrupted...`）。
- `frontend/src/features/classroom/api.ts`：新增 `getLatestJob(lectureId)` → `GET lectures/{id}/job`。
- `frontend/src/api/client.ts`：**移除全局 `Content-Type: application/json`**。原全局头会覆盖 ky 对 `FormData` 的 multipart 自动编码，导致页面录音/课件上传请求体缺 `file` 字段（后端 422 `Field required`）。JSON 请求均走 ky 的 `json` 选项（自动设置 application/json），故该全局头既冗余又有害。

### 3.2 后端

- `backend/app/services/job_service.py`：新增 `get_latest_asr_job(db, lecture_id)`（最新 type=asr 任务）与 `asr_job_to_response(job)`（对齐 `JobResponse`，`retryable = error_type in {rate_limited, timeout, provider_error}`，`completed_units = 1 if output_version else 0`）。
- `backend/app/api/lectures.py`：新增 `GET /api/lectures/{lecture_id}/job`（`JobResponse | None`），供页面刷新后恢复最新任务状态。

### 3.3 验证脚本（证据）

- `backend/scripts/serve_phase2_browser.py`（新建）：浏览器实测专用后端，复用 `app.main:app` + `FakeASRProvider` 可控替身；`POST /__test/asr-fail` / `/asr-ok` / `/asr-state` 切换替身结果；`POST /__test/dispatch` 按需派发一次 ASR（`recover_and_dispatch(provider=fake)`，仅 ASR、不触发 organize/index），精确控制「失败→重试」时序、避免自动重试与手动重试竞态。
- `backend/scripts/verify_phase2_browser.cjs`（新建）：Playwright + Edge 真实浏览器实测（无请求拦截/夹具），覆盖复验项 1/2/3 + A1a 请求审计。
- `backend/scripts/gen_trial_pdf.py`（新建）：生成 2 页已知页码/标记 PDF `newton-lesson.pdf`（第 1 页 `PAGE_ONE_MARKER_NEWTON_LAW_2026`、第 2 页 `PAGE_TWO_MARKER_F_EQ_MA_2026`），供「已知页码/标记」课件定位验证。

---

## 4. 对应验收编号（冻结判据逐字保留 + A1 拆分）

| 编号 | 操作（逐字） | 冻结通过条件与证据（逐字） | 结果 |
|---|---|---|---|
| **A1a** | 身份依赖替换下的 HTTP 请求（另一用户用已知标识直接读写）。 | 归属和数量正确，跨用户请求被拒且内容未改变。保存请求结果及前后快照。 | **通过**（服务层注入 `subject_id` 替身第二身份，见 §5.2 / §5.3） |
| **A1b** | 实际双用户会话（两个独立受信身份会话直接读写对方标识并核对前后快照）。 | 需受信 HTTP 身份入口区分两个会话主体。 | **阻塞**（`get_scope()` 恒返回 `local-user`，无 HTTP 身份入口，见 §9） |
| A2 / R2 | 从页面上传真实录音与课件，点选转写段；模拟一次转写失败后重试。 | 真实 ASR 产生有序时间段，播放器定位到对应原句，课件可定位原页；失败可见，重试得到一份有效结果。保存作业记录及定位截图。 | **通过**（真实 ASR 有序时间段 + 浏览器实测：点选定位 / 课件原页 / 失败可见 / 重试有效，见 §5.1 / §5.2） |

> A1 **不汇总为通过**：A1a 通过、A1b 阻塞，二者分开报告。

---

## 5. 验证结果

### 5.1 浏览器实测（本轮返工核心证据）— `verify_phase2_browser.cjs`

真实本地前端（Vite :5173）+ 真实本地后端（:8787，可控替身 ASR，按需派发），**无请求拦截/夹具**，`TOTAL 6 PASS 6 FAIL 0`。

| 检查项 | 结果 | 实测关键值 |
|---|---|---|
| 1-课时课件归属（首载/刷新/切换）+ 课程共享展示规则 | ✅ | 课时 1 只显示 `courseware-A`+`courseware-shared`（不含 B）；课时 2 只显示 `courseware-B`+`courseware-shared`（不含 A）；共享课件 `data-shared="true"`、`data-lecture-id=""`（徽标「课程共享」） |
| 2-首次点选转写段（刷新后点第三段 13.61s + 连续跳转） | ✅ | `segCount=5`；刷新后首次点第 3 段 `third_s=13.61`（期望 13.610）；点第 1 段 `first_s=0`；点第 4 段 `fourth_s=23.2`（期望 23.200） |
| 3a-页面课件定位原页（已知页码/标记） | ✅ | PDF 上传后 `blockCount=2`；第 1 页命中 `PAGE_ONE_MARKER_…`（`page_no=1`）、第 2 页命中 `PAGE_TWO_MARKER_…`（`page_no=2`）；页面显示「第 2 页」徽标 |
| 3bcd-页面录音上传 + 失败可见 + 刷新重试得有效结果 | ✅ | 上传→派发失败→页面显示 `retry_wait` +「供应商调用失败（provider_error）」；刷新后仍恢复 `retry_wait` 且重试按钮可见；重试→`succeeded`，`output_version` 从 `null` → 有效 UUID，转写段 `0→3`，有效输出版本 `active_transcript.version=1 / segment_count=3` |
| A1a-浏览器请求无身份头（服务端固定 local-user） | ✅ | 89 条 `/api/*` 请求均无 `authorization` 头，头部仅 `sec-ch-ua* / referer / user-agent / accept` |
| 浏览器无页面脚本错误 | ✅ | `count=0`（修复 `audio.play()` 拒绝后） |

作业记录（item 3）落盘于 `results.json`：`job_id`、`before {status: retry_wait, error: 供应商调用失败（provider_error）, outputVersion: null, activeTranscript: null}`、`afterRefresh {status: retry_wait, retryVisible: true}`、`retry {httpStatus: 200, jobStatus: queued}`、`after {jobStatus: succeeded, outputVersion: <uuid>, segments: 3, activeTranscript: {version: 1, segment_count: 3}}`。

### 5.2 模拟路径（可控替身 + 进程内 HTTP + 跨用户身份注入）— `verify_phase2_classroom_input.py`

**TOTAL 2 PASS 2 FAIL 0，退出码 0**（沿用初版结果，未改动后端）。

- **A1a（身份依赖替换下的 HTTP 请求）通过**：subject A 创建课程「验收课程-牛顿」+ 2 课时，`lecture_count=2`；刷新后数量正确。通过 `app.dependency_overrides[get_scope]` 把请求身份替换为 subject B，用已知 course_id / lecture_id 直接读写：课程列表不泄露（`b_leak=false`）、课时列表/详情/转写段/新建课时/删除课程/上传音频全部 `404`；切回 A 后 `before_snapshot == after_snapshot`（内容未改变）。此为「身份依赖替换下的 HTTP 请求」路径（服务层注入第二身份，HTTP 请求经 `ASGITransport` 进程内发出）。
- **A2（上传→转写→定位→失败→重试）通过**：真实 WAV 字节经 HTTP 上传（媒体校验真实通过）+ 真实 Markdown 课件（`parse_status=parsed`、`block_count=10`）；替身 ASR 派发后 `succeeded` 产出 3 个有序片段（0–1000 / 1200–2200 / 2400–3200ms）；课件 10 块全部 `paragraph_index` 定位；模拟失败（`poll_result="failure"`）→ `retry_wait`+`provider_error`；`POST /api/jobs/{id}/retry` → `200` → `succeeded` 产出 `output_version`。

### 5.3 真实路径（真实供应商）— `verify_phase2_real_http.py`

**TOTAL 2 PASS 2 FAIL 0，退出码 0**（沿用初版结果）。

- **真实-创建与上传 通过**：真实 HTTP 创建课程「真实链路验收课程-e75b76」+ 2 课时；上传真实录音 `newton-trial.wav`（201）；上传真实课件 `newton-trial.md`（201，`block_count=10`）。
- **真实-ASR与定位 通过**：真实 DashScope ASR 派发 `dispatched=1/succeeded=1/failed=0`，产出 `output_version`；经 HTTP 读回 **5 个有序片段**（0–3093 / 3093–13610 / 13610–23200 / 23200–35124 / 35124–45260ms），内容与已知旁白一致（含真实 ASR 少量同音字误差，反证非替身）；课件 10 块全部 `paragraph_index` 定位。

> 说明：`config.json` 的 `asr_mode=real` 是读取自 `backend/.env` 的环境值；模拟/浏览器脚本**实际派发时显式传入 `FakeASRProvider`**（`recover_and_dispatch(..., provider=fake)` / `run_job(..., provider=fake)`），从未调用真实供应商，不冒充真实调用。浏览器实测 item 2 用已有真实录音 `88e501fd-…wav`（45.28s 真实语音）与真实转写段（5 段，第三段 13.610s）。

---

## 6. 实际运行的命令及结果（复验可复跑）

以下命令涉及数据库口令（来自 `backend/.env`，`settings.DATABASE_URL`），脚本不打印、证据不落盘。

```powershell
# ── 浏览器实测（本轮返工核心证据）──
# 终端 A（backend 目录）：测试后端（可控替身 ASR，按需派发，:8787）
$env:PYTHONUTF8 = "1"; $env:PYTHONPATH = "."
python scripts/serve_phase2_browser.py

# 终端 B（frontend 目录）：Vite 开发服务器（:5173，/api 代理到 :8787）
npm run dev

# 终端 C（backend 目录）：真实浏览器实测（Node v24 + Playwright + Edge）
node scripts/verify_phase2_browser.cjs
# → 6/6 通过（item1 归属 / item2 点选 seek / item3a 课件原页 / item3bcd 失败重试 / A1a 请求审计 / 无页面错误）

# ── 模拟路径（隔离测试库，不触碰活跃库）──
# backend 目录
$env:PYTHONPATH = "."; $env:PYTHONUTF8 = "1"
python scripts/verify_phase2_classroom_input.py
# → TOTAL 2  PASS 2  FAIL 0（exit 0）

# ── 真实路径（需先启动真实 backend 连活跃库 :8787）──
python scripts/verify_phase2_real_http.py
# → TOTAL 2  PASS 2  FAIL 0（exit 0）

# ── 前端类型检查 + 全量构建 ──
# frontend 目录
npx tsc -b
npm run build
```

实测记录：

| 步骤 | 脚本 | 结果 | 退出码 |
|---|---|---|---|
| 浏览器实测（6 项） | `verify_phase2_browser.cjs` | 6/6 通过 | 0 |
| A1a/A2 模拟路径 | `verify_phase2_classroom_input.py` | `TOTAL 2 PASS 2 FAIL 0` | 0 |
| 真实 HTTP + 真实 ASR | `verify_phase2_real_http.py` | `TOTAL 2 PASS 2 FAIL 0` | 0 |
| 前端类型检查 | `npx tsc -b` | 通过 | 0 |
| 前端全量构建 | `npm run build` | 通过 | 0 |

---

## 7. 证据路径

| 证据 | 路径 |
|---|---|
| 浏览器实测结果（JSON，含 6 项检查 + 请求审计 + 错误） | `docs/classroom-agent/reset-v1/evidence/phase2/browser/results.json` |
| 课时 1 课件归属截图 | `docs/classroom-agent/reset-v1/evidence/phase2/browser/item1-lecture1.png` |
| 课时 2 课件归属截图 | `docs/classroom-agent/reset-v1/evidence/phase2/browser/item1-lecture2.png` |
| 首次点选转写段（13.61s seek）截图 | `docs/classroom-agent/reset-v1/evidence/phase2/browser/item2-seek.png` |
| 页面课件定位原页截图 | `docs/classroom-agent/reset-v1/evidence/phase2/browser/item3-doc.png` |
| 失败可见 + 刷新重试截图 | `docs/classroom-agent/reset-v1/evidence/phase2/browser/item3-retry.png` |
| A1a/A2 模拟结果（JSON） | `docs/classroom-agent/reset-v1/evidence/phase2/results.json` |
| 模拟路径配置说明 | `docs/classroom-agent/reset-v1/evidence/phase2/config.json` |
| 真实 HTTP + 真实 ASR 结果（JSON） | `docs/classroom-agent/reset-v1/evidence/phase2/real_results.json` |
| 已知页码/标记 PDF（第 1/2 页标记） | `docs/classroom-agent/trial-materials/newton-lesson.pdf` |

证据中**未含任何密钥或个人信息**（数据库口令、OSS/DashScope 密钥均不出现，仅以「已配置」表示；样例内容为公开物理知识点 demo）。

---

## 8. 真实 vs 模拟

- **真实**：PostgreSQL 16 真实写入；真实录音 `88e501fd-…wav`（45.28s）用于点选 seek；`newton-trial.wav` + `newton-trial.md` 上传；**真实 DashScope ASR**（付费调用，5 段真实时间区间）；**真实 OSS 上传**；真实 HTTP 接口；**真实浏览器（Playwright + Edge）**实测页面交互（点选 seek / 课件上传 / 失败重试）；前端 `tsc` + `npm run build`。
- **模拟**：A1a 的「第二身份」用 `app.dependency_overrides[get_scope]` 注入 `subject_id`（HTTP 层无多用户身份，见 §9）；A2 的「转写失败→重试」与浏览器实测的 ASR 用 `FakeASRProvider` 可控替身（`poll_result=failure/success`）触发，非真实供应商故障。

---

## 9. 未验证项与阻塞原因

- **A1b「实际双用户会话」阻塞（明确登记，非汇总通过）**：`backend/app/api/deps.py` 的 `get_scope()` 恒 `return local_scope()`；`backend/app/services/scope.py` 的 `local_scope()` 恒 `ScopeContext(subject_id=settings.LOCAL_SUBJECT_ID)`，`LOCAL_SUBJECT_ID="local-user"`。**无受信 HTTP 身份入口**（无登录/session/token → 无按会话区分的主体），两个真实浏览器会话都会落到同一 `local-user`，无法通过 HTTP 层隔离对方标识。A1a 已用服务层 `subject_id` 注入的替身路径覆盖「归属 + 跨用户拒绝 + 内容不变」，但「实际双用户会话」需未来引入登录身份解析后方可验，本轮登记为阻塞。

### 9.1 A1b 阻塞的完整证据链（本次独立复核，逐项确认「无身份来源」）

本轮对「是否存在可接通的服务端身份来源」做了穷尽复核，结论为**不存在**：

1. **无用户/账号/会话/令牌模型**：`backend/app/models/__init__.py` 导出 30 个 ORM 模型（Course/Meeting/Transcript/…/ProviderPermitSlot），**无 User / Account / Subject / Session 模型**。
2. **无登录与令牌校验**：全后端 `grep 'login|jwt|access_token|get_current_user|verify_token|decode_token|X-User|X-Identity|Bearer'` 仅命中 —— `dashscope_asr_service.py` 的 `Authorization: Bearer {api_key}`（**出站**到 DashScope，非入站校验）与 `agents/harness/retry.py` 的 `authentication/unauthorized`（**供应商错误分类字符串**，非身份鉴权）。**无任何入站身份解析/校验代码**。
3. **无身份配置项**：`config.py` 中与身份相关的仅 `LOCAL_SUBJECT_ID="local-user"`（其余 `RATE_USER_*`/`BUDGET_SUBJECT_*` 是限流/预算命名，非身份）；无 `AUTH/TRUSTED/HEADER/MULTI` 类配置开关。
4. **`main.py` 仅 CORS 中间件**，无鉴权中间件、无请求头解析。
5. **前端 `client.ts` 存在遗留 `beforeRequest` 钩子**：若 `localStorage.access_token` 存在则附加 `Authorization: Bearer …`；但（a）后端**无任何代码校验该头**，（b）**无登录流程写入 `access_token`**——属未接线的死代码，不构成身份入口。

**身份解析（请求 → `subject_id`）是唯一缺失环节**：数据层的归属/隔离已完备——`Course.owner_id`/`AudioAsset.subject_id`/`KnowledgeDocument.owner_id`/`ChatSession.owner_id` 等列齐全，`access.py` 的 `course_accessible_cond`（`Course.owner_id == scope.subject_id`）与 404 强制已实现，且 A1a 已证明「替换 `subject_id` 后跨主体读写被正确 404、内容不变」。因此阻塞点**不是归属模型**，而是「如何把 HTTP 请求映射到主体」这一**产品决定**尚未作出。

### 9.2 需要的产品决定（阻塞解除前置条件，需产品侧拍板，未自行修改冻结判据）

`scope.py` 注释明确产品当前定位为「个人版固定本地主体，与未来登录身份共用同一 ScopeContext 检查路径」。要解除 A1b 阻塞，需在以下三者中择一并据此实施（**本轮未擅自实施任何一项**）：

| 选项 | 内容 | 改动面 | 是否改变产品定位 |
|---|---|---|---|
| **① 引入真实登录/账号/SSO** | 新增 User 模型 + 登录/session/JWT，`get_scope()` 从已验证身份解析 `subject_id` | 大（新增鉴权域 + 前端登录） | 是（个人版 → 多用户） |
| **② 信任网关注入身份头** | 前置反向代理/网关鉴权后注入受信头（如 `X-User-Id`），后端仅信任该头并映射 `subject_id` | 小（`get_scope()` 读一个受信头；需配套网关鉴权） | 是（需网关边界，仍可称「单用户部署」） |
| **③ 明确保持单用户** | 维持「个人版」定位，A1 通过判据仅覆盖 A1a（依赖替换证明归属/隔离正确），A1b 登记为「不适用（单用户）」 | 无 | 否（维持现状） |

无论选 ①/②，`get_scope()` 的替换点都在 `deps.py` 一处，`ScopeContext` 检查路径与数据层归属模型无需改动；这正是 `scope.py` 注释预留的「未来登录身份共用同一检查路径」。
- **真实供应商「失败→重试」未验**：真实 DashScope 本次一次性成功，未构造真实供应商故障；失败可见与重试由 `FakeASRProvider` 可控替身覆盖（A2 判据中的「模拟一次转写失败后重试」本身即为模拟场景，故用替身符合判据）。

---

## 10. 失败记录（本轮返工发现并修复）

- **【真实业务 bug，已修复】ky 全局 `Content-Type: application/json` 破坏 multipart 上传**：`frontend/src/api/client.ts` 曾全局设置该头，覆盖 ky 对 `FormData` 的 multipart 自动编码，导致**页面**上传录音/课件时请求体缺 `file` 字段（后端 422 `{"detail":[{"loc":["body","file"],"msg":"Field required"}]}`）。`curl`/httpx 显式 multipart 不受影响，故 HTTP 接口路径（初版）未暴露此问题；浏览器实测（从页面点选上传）暴露。**修复**：移除该全局头（JSON 请求走 ky `json` 选项自动设置 application/json）。
- **【真实小缺陷，已修复】`audio.play()` 未处理拒绝**：`lecture-detail-page.tsx` 的 `void audio.play()` 在快速连续点选时产生未处理 Promise 拒绝（浏览器 `The play() request was interrupted by a call to pause()`）。**修复**：改为 `audio.play().catch(()=>{})`。修复后「浏览器无页面脚本错误」`count=0`。
- **【测试夹具 bug，非业务缺陷】**：浏览器实测 item 3 初次用 `makeWav(2)`（2000ms）短于替身默认最大片段终点（3200ms），触发 `validate_segments` 的「片段终点超出音频时长」→ `invalid_result`。修正为 `makeWav(5)`（5000ms）。转写时长校验本身按预期工作。
- 真实 ASR 一次通过，无失败。模拟/浏览器失败-重试按判据通过。

---

## 11. 待办（阶段 2 复验通过前不进入阶段 3）

- 真实路径在活跃库 `yuan_classroom_local` 留下了验证数据：课程「真实链路验收课程-e75b76」+ 2 课时 + 1 音频资产（`88e501fd-…`）+ 1 转写版本（`ef38d285-…`）+ 1 课件，以及本轮浏览器实测的临时课程/课时/课件/音频资产。如需清理可 `DELETE /api/courses/{id}`（软删归档）或由验收方核验后删除；本次未擅自删除以保留真实证据。
- 浏览器实测后端 `serve_phase2_browser.py`（:8787）与 Vite dev server（:5173）在复验期间保持运行即可；验收完成后停止。
