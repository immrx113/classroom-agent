# 阶段 5 最终独立验收结论（reset-v1 · A1–A7 冻结清单）

日期：2026-11-11 ｜ 执行人：Claude（deepseek-v4-pro）｜ 范围：阶段 5 最终独立验收

> 本文件是阶段 5 冻结验收的**最终结论**。它把「工程回归」与「整体验收」分开记录：
> **工程回归全部通过**，**整体验收保持阻塞**。阻塞点仅剩冻结清单内未闭合的两处缺口
> （A2 真实链路证据、A7 实际旧数据副本），其中 A2 已按复用+重新执行补齐证据，A7 因无
> 历史数据保持阻塞。真实 LLM / 真实嵌入未验证仅作**说明记录**，不构成本次放行条件。

---

## 1. 结论摘要

| 维度 | 结论 |
|---|---|
| **工程回归** | **通过**（全部自动化回归脚本退出码 0，见 §2） |
| **整体验收（冻结清单 A1–A7）** | **阻塞**（A7 缺实际旧会议数据副本，见 §4；A2 已补齐，见 §3） |
| 真实 LLM（整理/问答）、真实嵌入 | **未验证**，作说明记录，非放行条件（见 §6） |

---

## 2. 工程回归通过清单（最终代码版本 alembic head `7d8e9f0a1b2c`）

| 编号 | 脚本 | 结果 | 退出码 |
|---|---|---|---|
| A1 空库迁移基线 | `verify_fresh_migration_phase1.py` | 8/8 | 0 |
| A1a / A2 资料输入（归属+上传+失败重试） | `verify_phase2_classroom_input.py` | 2/2 | 0 |
| A3 / A4 / A6 整理发布 | `verify_resetv1_phase3.py` | 20/20 | 0 |
| A5 问答检索闭环 | `verify_resetv1_phase4.py` | 7/7 | 0 |
| A7 旧数据迁移对照（构造样本） | `verify_olddata_migration_phase1.py` | 16/16 | 0 |
| A7 浏览器全流程（默认首页→上传→整理→发布→问答→引用回跳） | `verify_phase5_browser.cjs` | 9/9 | 0 |
| **A2 真实 ASR 转写段点击（最终页面，本轮新增）** | `verify_phase5_realasr_seek.cjs` | 3/3 | 0 |

> A1b（真实双用户会话）在 v1.1 已明确**不适用**（个人单用户版，产品决定），A1a 归属回归
> 由 `verify_phase2_classroom_input.py` 覆盖（跨用户读写 404 + 内容不变），不计入阻塞。

---

## 3. A2 证据补齐：复用 vs 重新执行

冻结判据（逐字）：*「真实 ASR 产生有序时间段，播放器定位到对应原句，课件可定位原页；
失败可见，重试得到一份有效结果。」*

先核对阶段 2 已有真实录音与 `real_results.json` 的**适用性**：活跃库 `yuan_classroom_local`
中真实课时 `4b141489-…` 的转写仍为 `provider=real`、`provider_task_id=7b4cd1b6-…`、5 个有序
时间段的 `transcript_segments` 与 `real_results.json` 逐字一致；真实录音 `newton-trial.wav`
已持久化为 `assets/88e501fd-….wav`（22.5kHz 单声道、45.28s，磁盘文件仍在）。**适用性成立**，
可复用。

| A2 判据元素 | 证据路径 | 复用 / 重新执行 | 结论 |
|---|---|---|---|
| 真实录音上传 | `trial-materials/newton-trial.wav` → `backend/uploads/assets/88e501fd-….wav` | **复用**（阶段 2 已上传，文件仍在） | 通过 |
| 真实 ASR 产生有序时间段 | `evidence/phase2/real_results.json`（5 段 + asr_stats）+ DB `transcript_versions(provider=real)` / `transcript_segments` | **复用**（阶段 2 真实 DashScope 调用已成功，本次**未重新计费**） | 通过 |
| 播放器定位到对应原句（点选转写段） | `evidence/phase5/realasr/results.json` + `item-seek.png`（**本轮新增**） | **重新执行**（最终页面版本浏览器实测，provider=real，5 段点选 seek 命中 0s/13.61s/23.2s，页面原句与真实 ASR 逐段一致） | 通过 |
| 失败可见 + 重试得有效结果 | `evidence/phase2/results.json`（A2 `failure_status=retry_wait` / `retry_final=succeeded`）+ `evidence/phase2/browser/results.json`（3bcd）+ `item3-retry.png` | **复用**（阶段 2 已记录；`job-status`/`job-error`/`job-retry` 组件在最终版本未变） | 通过 |
| 课件可定位原页 | `evidence/phase2/browser/item3-doc.png`（PDF 页码 1/2 定位） | **复用**（阶段 2 浏览器证据） | 通过 |

**供应商调用条件评估（A2）**：DashScope（`OPENAI_API_KEY` + `OPENAI_BASE_URL=dashscope…/compatible-mode/v1`，
`ASR_MODE=real`）与 OSS（`OSS_ACCESS_KEY_ID/SECRET`、`OSS_BUCKET_NAME=classagenttest`）配置在位，
`CLASSROOM_PAID_CALLS_ENABLED=true`。阶段 2 已据此完成一次真实 DashScope ASR 调用并成功（`real_results.json`
5 段、`provider_task_id` 持久化），故条件**充分**，无供应商阻塞；本轮为避免重复计费，**复用**该真实结果，
未重新发起付费调用。若后续要求「在最终页面版本上重新发起一次真实 ASR 付费调用」作为独立证据，需用户明确
授权并承担计费，届时可另跑 `verify_phase2_real_http.py`（当前它连活跃库，见其 docstring）。

---

## 4. A7 阻塞：无实际旧会议数据库副本

冻结判据（逐字）：*「对旧库副本执行迁移，与迁移前按标识比对……旧记录数量、内容和归属可核对，
无隐式归课。」*

- **现状**：`verify_olddata_migration_phase1.py` 的 16/16 用的是**构造的旧库状态**（3 条合成
  meeting：重复 `seq_index` / 负时间 / mock / real / 未知来源 / 缺失音频 / 倒置时间），验证的是
  **迁移逻辑与归属规则**，不是**真实历史记录**。
- **排查结论（穷尽）**：无真实历史会议数据可作副本——
  - 原项目库 `yuan_meet` 的 `meetings` / `rooms` / `transcripts` / `decisions` / `agent_runs` /
    `summaries` / `action_items` / `risks` / `knowledge_documents` 全表 **0 行**；
  - 文件系统无任何旧会议 dump/backup（`.sql` / `*dump*` / `*backup*` 仅命中 `.venv` 依赖包）；
  - `yuan_classroom_local` 内 meeting 均为阶段 2–5 创建的课堂课时，非迁移前的历史会议。
- **结论**：**阻塞**。按验收方要求「若确实没有历史数据，保持阻塞，等待用户作出适用范围决定」，
  本次不擅自定义「无历史数据=通过」。解除阻塞需用户选择其一：
  1. 提供真实旧会议数据库副本（含旧 `meetings` + `transcripts` 记录），我按旧记录标识（id）在
     隔离库做迁移前后数量/内容/归属比对；或
  2. 明确「本项目无历史会议数据，A7 迁移对照以构造边界样本为准」作为适用范围决定，据此将 A7 记为通过。

---

## 5. 本轮修改文件

- 新增验证脚本 `backend/scripts/verify_phase5_realasr_seek.cjs`（最终页面版真实 ASR 转写段点击定位）。
- 新增证据 `docs/classroom-agent/reset-v1/evidence/phase5/realasr/{results.json, item-seek.png}`。
- 迁移 `yuan_classroom_local` 至 alembic head `7d8e9f0a1b2c`（仅新增 `chunk_items.started_at/finished_at`
  两个可空列，无回填、无删除，既有成果保留）。

---

## 6. 说明记录（非放行条件）

- **真实 LLM（整理 / 问答）未验证**：A3/A4/A5/A7 的整理与问答在自动化与浏览器回归中均由
  `FakeOrganizeModel` / 确定性摘录取证，未接通真实整理/问答 LLM。这属语义质量（P1 人工项），
  **不在冻结清单放行条件内**，仅作说明，不追加为阻塞项。
- **真实嵌入未验证**：检索索引用 `DeterministicEmbedding(1024)`（`INDEX_EMBEDDING_PROVIDER=deterministic`），
  未接通真实 `text-embedding-v3`。同样作说明，不追加为阻塞项。
- **真实课堂录音/课件非用户真实数据**：`newton-trial.wav` / `newton-trial.md` 由本机生成（真实音频、
  真实 DashScope 转写，但**不是用户的真实课堂数据**），用于验证真实链路而非真实业务内容。属说明记录。

---

## 7. 下一步（停在阶段 5，等待最终复验）

- A2 证据已按「复用 + 重新执行」补齐（§3），无需再改。
- A7 阻塞等待用户作出适用范围决定（§4），解除后方可整体放行。
- 全部回归命令见 `15-phase5-entry-handoff.md` §6；本轮新增 `node scripts/verify_phase5_realasr_seek.cjs`
  复跑需先起真实后端（`uvicorn app.main:app` 连 `yuan_classroom_local`）+ 真实前端（`npm run dev`）。

2026-11-11 验收更新：用户确认无历史会议数据，现采用 [v1.2 冻结清单](03-acceptance-v1.2.md)。[最终独立验收](17-final-review-v1.2.md)已通过；本文此前的阻塞结论作为历史记录。
