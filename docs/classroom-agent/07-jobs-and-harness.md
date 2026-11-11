# 07 任务、Harness 与可观测性

[返回总览](README.md)

## 现状与问题

转写使用 BackgroundTasks，纪要请求直接 await 图执行。依赖声明中存在 Celery/Redis，但没有完整可靠任务链。Harness 有预算、超时、熔断与结构校验，智能退避未完整接入；内部异常可能被转换成普通输出，币种命名存在差异。现有人工审核直接放行，图未接入 checkpointer。

修改范围：`agents/harness/`、`agent_run_service.py`、`models/agent_run.py`、工作流入口、配置与部署。新增任务模型、worker、outbox 与统一模型调用网关。

## 持久任务模型

建议先使用项目已有依赖方向建立 Celery + Redis worker，数据库作为任务业务状态的事实来源。具体队列配置实施时按安装版本验证。

`processing_jobs`：id、type、resource_id、input_version、config_hash、status、stage、attempt、heartbeat_at、cancel_requested、error_code、run_id。

`job_steps`：job_id、step_key、input_hash、output_ref、status、attempt、started_at、finished_at。

`outbox_events`：事务内创建的待投递事件，包含唯一业务键、投递状态与重试次数。

幂等键建议为 `resource_id + job_type + input_version + pipeline_version + config_hash`。队列按至少一次投递设计，不能把不重复投递作为前提。写出产物依赖唯一约束、输入版本和条件更新，避免重跑覆盖新数据。

## 状态与恢复

任务状态：queued → running → succeeded / failed / cancelled；retry_wait 表示等待下次尝试。人工确认使用 needs_review，不混作基础执行失败。

- 接口提交任务与 outbox 同事务落库；派发器投递成功后确认，重复投递仍可幂等处理。
- worker 获取租约并续心跳；失联任务经超时检查后重入队。
- 外部 ASR 使用供应商 task_id 恢复，模型生成使用已提交 step 产物恢复。
- 版本发布使用条件切换与事务；旧输入任务完成后不能覆盖已发布的新输入结果。
- 取消在阶段边界检查；已发生外部调用仍记录成本，必要时等待其结果后丢弃发布。
- 数据库阶段恢复与 LangGraph checkpoint 分开设计；M1 先实现阶段恢复，M2 再按需要接 checkpoint，不重复宣称完全等价。

## Harness 修复

1. **统一调用出口**：所有模型请求经过同一网关，携带 run_id、job_id、node、attempt 和 request_id。
2. **明确结果类型**：使用成功、空结果、可重试失败、不可重试失败、预算终止，不以返回空数组统一兜底。
3. **重试**：将分类退避接到实际调用；限制总尝试次数，遵守供应商重试提示。Schema 错误把具体校验问题放入修正输入。
4. **熔断**：仅真正成功才记 success；按供应商/模型等故障域管理，避免单一全局状态误伤无关调用。
5. **预算**：统一币种字段和计价版本；调用前检查并为并发请求预留估算额度，调用后按实际用量结算。预留与结算需原子化。
6. **预算边界**：在途请求可能已经产生成本，使用 token 输出限制和并发上限控制超支；不承诺无条件绝对上限。
7. **计量**：超限、失败和取消也保存已知消耗；供应商未返回 usage 时标记未知或估算，不记作零成本。
8. **上下文**：ContextVar 在入口设置 token 并在 finally reset，避免同一执行上下文复用造成串 run。
9. **状态一致**：重试成功更新当前节点状态，历史失败留在 attempt 记录；汇总状态不只看累计 errors 是否为空。

人工确认首版用于“编辑后发布笔记”等真实产品行为；如果需要图中暂停再继续，必须补齐持久 checkpoint、恢复入口和权限检查后才启用。

## 监控与接口

记录排队/运行时间、分块覆盖、节点尝试、provider request_id、输入输出 token、成本币种、检索候选与降级、引用校验、版本切换。日志避免默认记录整段课堂和凭证，保留排障所需摘要与关联 ID。

目标接口：`GET /jobs/{id}`、`POST /jobs/{id}/retry`、`POST /jobs/{id}/cancel`；AgentRun 保留内部诊断页面，业务用户主要看到阶段、进度、失败原因和重试入口。

验收：worker 中途退出可恢复；队列重复投递无重复产物；预算超限不进入新分支；限流有退避；取消保留已发生成本；任务状态与真实产物一致。

依赖：模块 01/10；被其余处理模块复用。
