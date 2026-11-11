# 阶段 3 交接说明 · 课堂整理与发布（reset-v1 · 返工第九版）

日期：2026-11-11 ｜ 执行人：Claude（deepseek-v4-pro）｜ 范围：仅阶段 3（A3 / A4 / A6），停在阶段 3 等待**独立复验**。

> 本版为第九次独立验收返工后的交接说明。前八版（草稿优先、重复发布幂等、预算控制、分支调度修正、编辑后发布重新核对、运行详情端点、生产规划策略、只改正文识别、发布保留章节、正文核对基线补齐、旧草稿锚点核对、旧草稿多字段条目、逐字段核对、重复引文定位）均已通过；本版集中处理 **A4 只改引文** 一项返工，保留已通过的 A3 / A6 / A4i（单字段结论）：
> 1. **只改引文（正文与原证据 ID 不变，本版修正）**：第八版在「优先用原证据定位」锁定片段后，直接沿用条目的 `quote` 创建有效证据，未核对当前引文是否确实存在于该所属片段——客户端只把 `quote` 改成转写中不存在的标记时，该标记会被保存为有效引文。本版改为：**创建有效证据前，核对当前引文确实存在于该条目所属片段**；失配则**拒绝该引文**，优先**恢复原证据定位里可核实的原引用**（在该片段内逐字命中），无法恢复则降 `pending` 并清空有效证据。不跨片段用其他片段中的文字替当前片段背书。
> 2. **保留已通过的 A3 / A6 / A4i（单字段结论）回归**不变。

---

## 1. 本阶段完成内容（摘要）

将「全课整理」改造为**四类课堂整理产物 + 可审阅草稿 + 显式发布**，并对齐冻结清单 A3 / A4 / A6：

- **规划与四类分支（R3）**：整理入口先按输入做**本地规划**（`plan()`），再逐块抽取。分支选中/跳过/执行与计数写入产物 `plan` / `branches`，取自**实际运行**。四类分支为 `笔记（note）` / `知识点（knowledge_point）` / `例题（example）` / `重点难点（key_point）`；缺材料的列表为空，不编造题目/知识点。
- **生产规划策略（R3）**：`OrganizeModel.plan`（生产/基类）从「四类全部尝试」改为**实际策略**——按通用课堂关键词检测各分支是否有材料，无材料跳过；`meta` 携带 `available_budget_micro` / `branch_cost_micro` 时按优先级（笔记 > 知识点 > 例题 > 重点难点）裁剪。
- **分支调度与执行状态（R3）**：`_selected_branch_names(plan)` 决定选中分支；`executed` 取自真实运行事件。被跳过分支 `executed=false`，与「选中但产物为空」`executed=true、count=0` 严格区分。
- **可审阅草稿（R4）**：完整整理先生成 **draft** 草稿，**不自动发布**。首次显式确认前不切发布指针、不建正式索引意向、不把草稿加入正式来源。
- **失效引用与编辑后重新核对（R4）**：模型不可伪造证据 ID / 回听时间；越范围片段、不匹配引用一律降「待确认（pending）」并从正式知识中排除。发布前 `_reverify_note_evidence` 逐条重新核对。
- **发布保留章节（R4）**：发布前重新核对后，`sections` 不再坍缩为单章——保留章节数量、标题、顺序与要点归属，仅按重新核对后的 `key_conclusions` 更新各节 `points`；正文变化时按旧证据 id 归属映射，使章节正文同步结论状态。
- **正文核对服务端基线（R4/R5）**：条目创建时固化 `_verified_body` 正文签名；编辑接口整体替换条目字典时按稳定身份（锚点键 → 证据 id → quote）把旧服务端基线带过。对**无签名的升级前旧草稿**，发布时**不以当前正文自建基线**，改以可信 `quote` 核对锚点字段。
- **旧草稿兼容（R5）**：无 `_verified_body` 的旧草稿条目，锚点字段（text/definition/problem/point）仍与 `quote` 一致 → 原样可核实，保留 `confirmed`；锚点字段已偏离 `quote` → 正文被改且无法核实，留在 `pending` 并从正式知识排除。客户端回传的内部签名在 `edit_note` 一律剥离。
- **旧草稿多字段条目（R6/R7）**：发布前核实会进入正式知识的全部正文字段。旧草稿条目在锚点字段与 `quote` 一致时保留 `confirmed`，并**逐字段**核对次要字段（例题 `derivation` / 知识点 `applicability` / 重点难点 `reason`）——能从来源片段逐字核实的原样内容保留进发布版本与正式索引；无法核实的字段清空。核对不跨片段。
- **重复引文定位（R8，上一版）**：发布重建证据前先保留**原证据定位**（服务端可信、属于当前转写版本的原 `EvidenceSpan`），发布时**优先用原定位**锁定条目所属来源片段，避免同一引文在多片段出现时错定位到预告/其他片段；原定位缺失且同一引文命中多处（歧义）→ 留待核实（`pending`）。
- **只改引文（R9，本版新增）**：创建有效证据前，核对当前引文确实存在于条目所属片段；失配则拒绝该引文，优先恢复原证据定位里可核实的原引用，无法恢复则降 `pending` 并清空有效证据。不跨片段用其他片段中的文字替当前片段背书。
- **重复发布（R4）**：重复发布仅一份有效发布结果 + 单一索引意向（幂等）。
- **运行详情（R6）**：`GET /api/jobs/{job_id}/run-detail` 返回 `plan` / `branches` / `chunks` / `shared_calls` / `provider_calls`；用量区分「零调用（预算阻止）」与「未知用量（供应商未返回 token 回执）」。
- **预算与超时（R6）**：预算不足阻止超额调用；供应商超时按 `ORGANIZE_MAX_ATTEMPTS=3` 有界重试，恢复后无重复产物。

20 项自动化验证全部通过（`TOTAL 20 PASS 20 FAIL 0`），证据落盘于 `evidence/phase3/`。

---

## 2. 工作区 / 版本标识

- 仓库：`C:\Users\11322\Desktop\cs\project\yuan-meeting-main`（**非 Git 工作树**，无提交号；以 alembic 迁移版本 + 源码 SHA256 作为版本标识）。
- 后端 Alembic **head = `7d8e9f0a1b2c`**（本版**无新增迁移**）。
- 验证运行于**隔离测试库** `yuan_resetv1_p3_<uuid>_main`（脚本创建、跑完即删），**未触碰活跃库 `yuan_classroom_local`**。
- 基础设施：`docker compose up -d`（`yuan_meet_postgres:5432` + `yuan_meet_redis:6379`），本阶段验证仅依赖 PostgreSQL。
- 后端运行 Python：`backend\.venv\Scripts\python.exe`（Python 3.12.10）。

---

## 3. 修改文件（本轮返工）

### 3.1 后端业务代码

- `backend/app/services/organize_service.py`（**本版核心改动**）：
  - `edit_note`：应用 patch 后、`_preserve_verified_baselines` 前，**剥离客户端可能回传的内部 `_verified_body`**（伪造基线不作核实依据），只保留服务端从 `old_content` 带过的可信基线。
  - `_reverify_note_evidence` 正文变化检测（A4 v5 / v6）：
    - 有服务端基线（`_verified_body`）则比较签名（变更→降 `pending` + 清空 `evidence_ids`）。
    - **无基线（升级前旧草稿）**：不以当前正文自建基线；以可信 `quote` 为依据——`quote_raw` 与锚点字段 `anchor_val` 均非空且不一致 → 正文被改且无法核实 → 降 `pending` + 清空 `evidence_ids`；一致则继续按 `quote` 逐字核对来源。
    - **无基线 + 锚点一致（A4 v6 / v7）**：对多字段条目，确定来源片段后，对进入正式检索却无 verbatim 锚点的次要字段（`_BODY_FIELDS` 中 `!= key_field` 的字段，即例题 `derivation` / 知识点 `applicability` / 重点难点 `reason`）**逐字段与该来源片段核对**——能从片段逐字核实的保留，无法核实的清空；单字段结论（`key_conclusions.text == key_field`）不受影响。核对锁定在确定片段本身，不跨片段。
    - **来源定位（A4 v8，上一版）**：`_clear_note_evidence` 前先抓取原 `EvidenceSpan` 的 `segment_start` / `start_ms` / `end_ms`（仅限 `transcript_version_id == note.transcript_version_id`）；重建时**优先用原定位**确定来源片段，原定位缺失才按 `quote` 回退，且命中多于一处置为歧义 → `pending`。
    - **引文核对（A4 v9，本版）**：用原定位（或回退匹配）锁定片段 `seg` 后，创建有效证据前核对当前 `quote` 是否确实存在于 `seg["content"]`；失配时若原证据定位里可核实（`original_span_loc[eid]["quote"]` 且 `segment_start == seq` 且原引用在 `seg` 内逐字命中）则恢复原引用（`quote` 与 `entry["quote"]` 一并恢复），否则降 `pending` + 清空 `evidence_ids`。
  - 章节保留（`_reverify_note_evidence` 末尾）+ `old_evidence_to_kc` 归属映射 + `flag_modified` 落库保持上一版不变。
- 其余（`organize_model.py` 生产规划策略、`index_service.py` 仅取 `confirmed`、`retrieval_service.py` 同步、`ChunkItem` 计时列、`GET /run-detail` 端点等）保持上一版不变。

### 3.2 验证脚本（证据，重写）

- `backend/scripts/verify_resetv1_phase3.py`：隔离库 + 可控替身模型，覆盖 **20 项**——A3（含例题/无例题/长输入多块 + 生产规划器两种材料对照 + 预算影响裁剪）、A4（失效引用边界 + 草稿优先 + 重复发布三状态 + 编辑后发布重新核对 + 只改正文保留原引文 + 发布保留章节 + 无签名草稿基线重建 + 正常编辑仅业务字段 + **旧草稿兼容组合回归** + **旧草稿多字段条目回归** + **只改引文回归**）、A6（超时重试 + 预算零调用 + 网关成功逐操作用量 + HTTP 运行详情）。

---

## 4. 对应验收编号（冻结判据逐字保留）

| 编号 | 操作（逐字） | 冻结通过条件与证据（逐字） | 结果 |
|---|---|---|---|
| **A3 / R3** | 分别处理含例题与无例题的材料，查看规划和各分支输出。 | 记录选中或跳过的分支；有例题时题干、步骤与原文一致，无例题时无编造题目。逐类抽核一条非空产物及来源。 | **通过**（§5 A3a / A3b / A3c / A3d / A3e） |
| **A4 / R4** | 注入失效引用，查看校验结果；用有效草稿发布两次；编辑为原文不支持内容后显式发布。 | 失效引用被拦截或修复，确认前草稿不能作为已发布知识召回；编辑后发布由服务端重新核对正文与来源（含只改正文保留原引文的实质变化、只改引文的失效识别、发布保留章节结构、无签名草稿与仅业务字段编辑的基线重建、旧草稿兼容、旧草稿多字段条目的全字段核实），失配条目重新待核实或排除在正式知识之外；重复提交仅产生一份有效发布结果。留存校验记录、版本及索引查询。 | **通过**（§5 A4a–A4k） |
| **A6 / R6** | 分别制造预算不足及供应商超时，检查运行详情后恢复服务重试；用作业 ID 查询运行详情。 | 预算阻止超额调用；重试次数符合配置，终态可见，可查分支耗时、错误与用量（含共享调用标识与实际记账），恢复后无重复产物。留存配置与调用记录。 | **通过**（§5 A6a / A6b / A6c / A6d） |

> A3 / A4 / A6 均**通过**，无阻塞项。A1 / A2 / A5 / A7 不在本阶段范围（A5 属阶段 4 检索闭环）。

---

## 5. 验证结果（`verify_resetv1_phase3.py`）

`TOTAL 20 PASS 20 FAIL 0`，退出码 0。逐项关键值（完整落盘于 `results.json`）：

| 检查项 | 结果 | 关键值 |
|---|---|---|
| A3a 含例题（HTTP 入口 + 派发） | ✅ | 例题 `problem=计算 2+3` / `derivation=2+3=5`、知识点 `definition=加法的定义` / `applicability=整数相加`、难点 `point=进位容易出错` / `reason=课本第三章例题`、笔记 `text=本堂课总纲：掌握加法`；四类各 1 条且各带 1 个 `evidence_id`；`branches` 四类 `selected+executed, skipped=false, count=1`；`extract_calls=1` + `summarize_calls=1` |
| A3b 无例题（HTTP 入口 + 派发） | ✅ | `examples=[] knowledge_points=[] key_points=[]`（不编造）；笔记 `selected=true, executed=true, count=2`；其余三类 `selected=false, skipped=true, executed=false, count=0` |
| A3c 长输入多块 | ✅ | `chunk_count=12`；`extract_calls=12`；例题/知识点 `selected=true`、难点 `selected=false`；难点 `skipped=true, executed=false, count=0` |
| A3d 生产规划器两种材料 | ✅ | 材料 1 → `selected=executed={note, knowledge_point, example, key_point}`；材料 2 → `selected=executed={note}`；传给 `extract` 的集合 == 规划选中集合 == 最终执行记录；两种材料选中集合不同 |
| A3e 生产规划器预算影响 | ✅ | 无预算 `full={全部4类}`；预算够 2 分支 `mid={note, knowledge_point}`；预算仅够 1 分支 `tight={note}` |
| A4a 失效引用边界（唯一标记） | ✅ | 合法结论 `confirmed` + 证据；伪造 ID / 模型时间 / 越范围 / 不匹配 quote 四条 `INVALIDUNIQUEMARKER_B` 全部 `pending` + `evidence_ids=[]`；`invalid_in_index=false`、`invalid_hits_contain_marker=false` |
| A4b 草稿优先 + 已有已发布再生成新草稿 | ✅ | 整理后 `draft / active_note=None / intent=0`；发布后 `published / active_note=note1 / intent=1`；第二版 `draft / 指针仍 note1 / intent=0`；`gather_sources` 只含已发布 note1 |
| A4c 重复发布三状态（幂等） | ✅ | 确认前 `draft / None / 0`；确认后 `published / note / 1`；第二次提交后 `published / 1 / published_count=1` |
| A4d 编辑为原文不支持内容再显式发布 | ✅ | 正常编辑（改来源标签）→ `confirmed`；失效编辑 `EDITBADUNIQUEMARKER_D`（text/quote 均不支持）→ `pending`；`bad_in_index=false`、召回不含标记；重复发布幂等 |
| A4e 只改正文保留原引文（四类+章节正文） | ✅ | 结论/定义/推导/难点各只改正文（保留 quote 与 evidence_ids）→ 发布后均 `pending`；章节正文同步 `pending`；`EDITBODYONLYUNIQUEMARKER_E` 不进索引、召回不含标记；重复发布幂等 |
| A4f 发布保留章节 | ✅ | 两章（第一章=要点甲、第二章=要点乙）；编辑/首次发布/重读/重复发布四次结构均 `[("第一章",[甲]),("第二章",[乙])]`；两章要点仍 `confirmed` 且带证据 |
| A4g 无签名草稿基线重建 | ✅ | 剥离 `_verified_body` 后发布：未改动条目 `confirmed` + 证据 + `no_forged_baseline=true`（不以当前正文自建基线）；`NOSIGUNIQUEMARKER_G`（quote 失配）→ `pending`；`changed_in_index=false`、`intact_in_index=true`、召回不含标记 |
| A4h 正常编辑仅业务字段 | ✅ | 仅提交业务字段（改正文、保留 quote 与 evidence_ids、不含 `_verified_body`）→ 编辑接口保存旧基线，发布识别正文变化 → `pending`；`BIZFIELDONLYUNIQUEMARKER_H` 不进索引、召回不含标记 |
| A4i 旧草稿兼容组合回归 | ✅ | 无签名草稿保留对照条目「旧草稿要点甲」`confirmed` + 证据；仅改另一条正文（保留 quote 与 evidence_ids）→ `COMBINEDUNIQUEMARKER_I` 条目 `pending` + `evidence_ids=[]`；`changed_in_index=false`、`intact_in_index=true`、`changed_hits_contain_marker=false` |
| A4j 旧草稿多字段条目 + 重复引文定位（本版） | ✅ | 「先预告题干、后正式讲解」：seq0 预告「预告：今天要讲计算 2+3」含「计算 2+3」但无推导，seq1 讲解「【例题】计算 2+3【解】2+3=5」两者皆含——同一引文命中两处。发布后未改对照例题 `derivation=2+3=5` 保留（confirmed），证据 `segment_start=1 / segment_end=1 / start_ms=2000 / end_ms=3000` 指向正式讲解片段（非 seq0 预告）；知识点 `applicability=整数相加`、难点 `reason=课本第三章` 保留；被改条目次要字段清空（`derivation=""` / `applicability=""` / `reason=""`）。索引双向边界：对照次要字段进索引、被改条目次要字段与三个唯一标记均不进索引；三路召回 `hits_contain_marker=false` |
| A4k 只改引文（本版） | ✅ | 从已确认草稿出发（两条例题 confirmed），正文与原证据 ID 完全不变，只把第一条例题的 `quote` 改为转写中不存在的唯一标记 `A4KQUOTEUNIQUEMARKER` 后经正常编辑+发布接口。发布后该条 `quote=计算 2+3`（标记未保存为有效引文）、`problem=计算 2+3` / `derivation=2+3=5`、`status=confirmed`、`span_quote=计算 2+3`（EvidenceSpan 引文恢复为原引用）；未改对照例题仍 `confirmed`（`problem=计算 4+5` / `derivation=4+5=9`）；`marker_in_index=false`、召回 `quote_marker_hits_contain_marker=false` |
| A6a 供应商超时重试 + 恢复 | ✅ | `retry_count=3 == ORGANIZE_MAX_ATTEMPTS`；终态 `failed / incomplete_coverage`、`chunk=failed / timeout`；运行详情 `note selected+executed count=0`、`chunks[0] error_code=timeout attempt=3`；恢复后 `succeeded / note_count=1` |
| A6b 预算不足（零调用） | ✅ | `failed / budget_exhausted`；`provider_calls=0 note_count=0`；`settled_micro=5000000 == cap`；运行详情 `note selected+executed=false`、`shared_calls=[] provider_calls=[]`；`zero_call=true` |
| A6c 网关成功（逐操作用量） | ✅ | ProviderCall `organize_extract` + `organize_summarize` 各 1 次，`usage_state=confirmed_sent / reserved_amount=1000000 / settled_amount=1000000 / has_timing=true`；`usage=null`（替身无 token 回执 → 未知用量） |
| A6d HTTP 运行详情 | ✅ | `branches`（note/example/knowledge_point `selected+executed`、key_point `skipped+not-executed`）；`shared_calls` 一次 `extract` `shared_branches={note, example, knowledge_point}`；`provider_calls` 条数 == attempts 之和；每块 `started_at/finished_at` 非空 |

---

## 6. 实际运行的命令及结果（复验可复跑）

命令涉及数据库口令（来自 `backend/.env` 的 `settings.DATABASE_URL`），脚本不打印、证据不落盘（`config.json`/`run.json` 仅存隔离库**名称**，不含连接串）。

```powershell
# backend 目录（脚本已自动把 backend/ 加入 sys.path，无需手工设 PYTHONPATH）
.\.venv\Scripts\python.exe scripts\verify_resetv1_phase3.py
# → TOTAL 20  PASS 20  FAIL 0（exit 0）
```

实测记录：

| 步骤 | 脚本 | 结果 | 退出码 |
|---|---|---|---|
| A3/A4/A6 二十项自动化 | `verify_resetv1_phase3.py` | `TOTAL 20 PASS 20 FAIL 0` | 0 |

脚本自建隔离库 → `alembic upgrade head`（`7d8e9f0a1b2c`）→ 跑 20 项 → 落盘证据 → 删除隔离库。**不触碰活跃库**。

**从课堂入口发起的复跑路径**（脚本内部实现，验收方可直接复跑整个脚本）：
1. `POST /api/lectures/{id}/organize` 创建整理 job（HTTP 实际入口）；
2. `organize_service.recover_and_dispatch_organize(db, model=<替身/生产规划器替身>)` 派发执行（worker 实际入口）→ 产出 **draft**；
3. 检查草稿与 `plan` / `branches` 规划记录；
4. （A4d/A4e/A4h/A4k）`PATCH /api/notes/{id}` 编辑；（A4f）编辑建立两章；（A4g/A4i/A4j）构造无签名旧草稿；（A4i/A4j）经正常编辑接口只改一条正文/次要字段；（A4k）经正常编辑接口只改一条 `quote`；
5. `POST /api/notes/{id}/publish` 显式发布（发布前重新核对 + 保留章节 + 基线核对）；
6. 查询结果（`gather_sources` + `retrieval_service.search` 边界）；`GET /api/jobs/{id}/run-detail`。

---

## 7. 证据路径

| 证据 | 路径 |
|---|---|
| 二十项结果（JSON，含逐项 detail） | `docs/classroom-agent/reset-v1/evidence/phase3/results.json` |
| 配置摘要（预算/重试/隔离库名，无口令） | `docs/classroom-agent/reset-v1/evidence/phase3/config.json` |
| 运行记录（起止时间/退出码/隔离库名） | `docs/classroom-agent/reset-v1/evidence/phase3/run.json` |
| 源码 SHA256 快照（版本标识） | `docs/classroom-agent/reset-v1/evidence/phase3/source-manifest.json` |
| 产物样例：含例题笔记 | `docs/classroom-agent/reset-v1/evidence/phase3/products/a3_with_example_note.json` |
| 产物样例：无例题笔记 | `docs/classroom-agent/reset-v1/evidence/phase3/products/a3_without_example_note.json` |

证据中**未含任何密钥或口令**（数据库口令不出现；样例内容为公开算术知识点 demo，无个人信息）。

---

## 8. 真实 vs 模拟

- **真实**：PostgreSQL 16 真实写入（隔离库）；`alembic upgrade head` 真实迁移；HTTP 请求经 `ASGITransport` 进程内真实发出；预算账户/预留/结算为真实持久账本语义；网关路径 `provider_calls` 真实落库。**生产规划策略 `OrganizeModel.plan` 是真实业务代码**。A4g/A4i/A4j 的「无签名草稿」通过直接 DB 层剥离 `_verified_body` + `flag_modified` 构造（模拟升级前数据，不经过编辑接口）；A4i/A4j 的正文/次要字段改动经 `PATCH /api/notes/{id}` 真实编辑接口。
- **模拟**：**全部外部模型调用均为可控替身**（`FakeOrganizeModel` / `FakeWithProdPlan` / `CraftedModel` / `TimeoutModel`），**无任何真实供应商（DashScope）调用**——A3/A4/A6 验证的是整理管线机械正确性，语义质量属 P1 人工项，且 `CLASSROOM_PAID_CALLS_ENABLED=False`。
- **预算不足**由「把 `subject_daily` 账户 `settled_micro` 预置为上限」触发；**供应商超时**由 `TimeoutModel` 抛 `ModelError(connection, retryable=True, code="timeout")` 触发。
- **恢复服务重试**：A6a 用 `retry_job`（生产重试入口）把 `failed` 重置为 `queued` 后重跑，验证无重复产物。

---

## 9. 未验证项

- **真实供应商语义质量（P1 人工项）**：四类分支语义是否准确未用真实模型核对，属 P1。
- **A5 / A7** 不在阶段 3 范围（A5 属阶段 4 检索闭环；A7 属迁移/端到端）。
- 预算路径本次仅验证 `subject_daily` 账户耗尽；`global_daily` / `trial_total` 跨账户 min 语义沿用 S3 已验逻辑。
- **供应商 token 级真实用量回执**因替身模型不返回 `usage` 而保持 `null`（未知用量），真实用量核对属 P1/开启付费调用后的人工项。
- **旧草稿的「锚点字段 == quote」核实口径**：对升级前无签名草稿，服务端以「锚点字段仍与 quote 一致」判定「原样可核实」，偏离则判定「无法核实 → pending」。对真实模型（正文为摘要、quote 为逐字原文，二者本就不等）的历史草稿，此口径会保守地将其留在 pending——这是「缺少可信基线且无法核实」的既定行为，属预期。
- **旧草稿多字段条目的次要字段核对口径（上一版）**：例题 `derivation` / 知识点 `applicability` / 重点难点 `reason` 在创建时即无 verbatim 锚点，升级前草稿也无 `_verified_body` 基线；发布时优先用原证据定位锁定条目所属来源片段，再逐字段与该片段核对——能从片段逐字核实的保留，无法核实的清空。对真实模型（次要字段为摘要，与原文本就不等）的历史草稿，此口径会保守地丢弃这些次要字段——同属「缺少可信基线且无法核实」的既定行为，属预期。
- **重复引文的归属口径（上一版）**：原定位缺失（无原证据、或原证据已不属于当前转写版本）且同一引文命中多处时，无法确定条目归属 → 留待核实（`pending`）；不跨片段用其他片段中的相同文字作依据。
- **只改引文的核对口径（本版）**：创建有效证据前，核对当前引文确实存在于条目所属片段；失配则拒绝该引文，优先恢复原证据定位里可核实的原引用（在该片段内逐字命中），无法恢复则降 `pending` 并清空有效证据。对客户端仅把 `quote` 改为转写中不存在标记的草稿，发布后该标记不会作为有效证据保存，标记不进入索引/召回。

---

## 10. 失败记录（返工与编写脚本时发现并修复）

- **只改引文未核对片段（业务 bug，本版）**：第八版在「优先用原证据定位」锁定片段后直接沿用条目 `quote` 创建有效证据，未核对当前引文是否确实存在于该片段；客户端只把 `quote` 改为转写中不存在的标记时，该标记会被保存为有效引文。修复：创建有效证据前核对 `quote` 确实存在于锁定片段；失配则拒绝该引文，优先恢复原证据定位里可核实的原引用，无法恢复则降 `pending` 并清空有效证据。
- **重复引文按 quote 全表扫描错定位（业务 bug，上一版）**：第七版发布重建证据时按 `quote` 全表扫描取**第一个**命中片段，「先预告题干、后正式讲解」时把条目错定位到预告片段（不含推导），导致未改对照例题的推导被错误清空。修复：重建前先保留原证据定位（服务端可信、属于当前转写版本的原 `EvidenceSpan`），发布时优先用原定位锁定片段；原定位缺失且 quote 命中多处 → 留待核实。
- **旧草稿多字段条目次要字段一律清空导致内容丢失（业务 bug，上一版）**：第六版对旧草稿多字段条目的次要字段**一律清空**，导致能从来源片段逐字核实的原样内容被错误丢失。修复：逐字段核对次要字段——能从片段逐字核实的保留，无法核实的清空；不跨片段。
- **旧草稿多字段条目次要字段未经核实进索引（业务 bug，上一版）**：仅以「锚点字段 == quote」核对，未覆盖会进入正式检索的次要字段。修复：无基线旧草稿条目锚点一致时，对次要字段逐字段核实，无法核实的清空，只保留可核实字段。
- **旧草稿被误判 confirmed（业务 bug，上一版）**：无 `_verified_body` 条目在发布时用「本次待发布正文」为自身建立基线（循环自证），导致「只改正文、保留原引文和证据」的旧草稿改动仍被当作 confirmed、标记进入索引/召回。修复：无基线条目不以当前正文自建基线，改以可信 `quote` 核对锚点字段（一致→confirmed，偏离→pending）；编辑接口剥离客户端回传的内部签名。
- **客户端回传内部签名可伪造基线（业务 bug，上一版）**：`edit_note` 直接沿用客户端可能回传的 `_verified_body`，使其成为核实依据。修复：应用 patch 后先剥离内部签名，仅由 `_preserve_verified_baselines` 从服务端 `old_content` 带过可信基线。
- **发布丢失章节（业务 bug，上一版）**：`sections` 坍缩为单章。修复：保留章节结构 + 重核映射（text 先匹配，text 变时按旧证据 id 归属）。
- **编辑丢基线 + 无签名跳过正文检查（业务 bug，上一版）**：修复：编辑按稳定身份带过基线；无签名重建基线=当前正文（本版进一步改为不以当前正文自建基线）。
- **A4 只改正文未识别 / A3 生产规划占位 / A4d-A4e 语义重叠 / JSONB 就地变更不落库 / 分支 executed 硬编码 / 运行详情查询时机 / 脚本 sys.path / 恢复重试未重置终态 / 证据暴露连接串**：均已修复（详见前几版交接说明）。

---

## 11. 待办（阶段 3 独立复验通过前不进入阶段 4）

- 停在本阶段等待独立复验；验收方按 §6 命令复跑 `verify_resetv1_phase3.py` 即可得到 `20/20`。
- 如需人工抽查：四类产物样例见 `evidence/phase3/products/*.json` 与 `results.json`；章节保留的四次结构对照见 A4f；无签名草稿与旧草稿兼容的边界见 A4g/A4i；旧草稿多字段条目 + 重复引文定位（先预告题干后正式讲解、证据定位与逐字段核对）见 A4j；只改引文的失效识别见 A4k；运行详情对照见 A6d。
