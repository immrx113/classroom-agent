import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, Coins, DollarSign, Clock, AlertCircle, ChevronDown, ChevronRight } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  AgentRunStatusBadge,
  ReviewStatusBadge,
} from '../components/agent-run-status-badge'
import { ReviewDialog } from '../components/review-dialog'
import { useAgentRun } from '../hooks/use-agent-runs'
import { formatDateTime } from '@/lib/utils'
import type { AgentRunStep, ToolCall } from '@/types'

// 状态 → 圆点颜色 class + 标签徽章 class
const STEP_STATUS_STYLES: Record<string, { dot: string; badge: string }> = {
  running: { dot: 'bg-blue-500', badge: 'bg-blue-100 text-blue-700' },
  succeeded: { dot: 'bg-green-500', badge: 'bg-green-100 text-green-700' },
  failed: { dot: 'bg-red-500', badge: 'bg-red-100 text-red-700' },
  timeout: { dot: 'bg-orange-500', badge: 'bg-orange-100 text-orange-700' },
  skipped: { dot: 'bg-gray-400', badge: 'bg-gray-100 text-gray-700' },
  budget_exceeded: { dot: 'bg-red-600', badge: 'bg-red-200 text-red-800' },
  invalid_output: { dot: 'bg-yellow-500', badge: 'bg-yellow-100 text-yellow-700' },
}

const STEP_STATUS_LABELS: Record<string, string> = {
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  timeout: '超时',
  skipped: '跳过',
  budget_exceeded: '预算超限',
  invalid_output: '校验失败',
}

export default function AgentRunDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { data: run, isLoading } = useAgentRun(id)
  const [reviewOpen, setReviewOpen] = useState(false)

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-40" />
        <Skeleton className="h-64" />
      </div>
    )
  }

  if (!run) {
    return (
      <div className="flex flex-col items-center justify-center py-20">
        <AlertCircle className="h-10 w-10 text-muted-foreground/50" />
        <p className="mt-3 text-sm text-muted-foreground">课堂处理记录不存在</p>
        <Button className="mt-3" variant="outline" onClick={() => navigate('/agent-runs')}>
          返回列表
        </Button>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* 顶部 */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="icon" onClick={() => navigate('/agent-runs')}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold">课堂处理详情</h1>
              <AgentRunStatusBadge status={run.status} />
              {run.review_status && <ReviewStatusBadge status={run.review_status} />}
            </div>
            <p className="mt-0.5 font-mono text-xs text-muted-foreground">{run.id}</p>
          </div>
        </div>
        {run.status === 'paused' && run.review_status === 'pending' && (
          <Button onClick={() => setReviewOpen(true)}>审批</Button>
        )}
      </div>

      {/* 基本信息 */}
      <Card>
        <CardContent className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-4">
          <InfoItem label="课时 ID" value={run.meeting_id.slice(0, 12) + '…'} />
          <InfoItem label="Graph" value={run.graph_name} />
          <InfoItem
            label="开始时间"
            value={formatDateTime(run.started_at)}
          />
          <InfoItem
            label="结束时间"
            value={run.finished_at ? formatDateTime(run.finished_at) : '—'}
          />
          <InfoItem label="当前节点" value={run.current_node ?? '—'} />
          <InfoItem
            label="审批人"
            value={run.reviewer ?? '—'}
          />
          <InfoItem
            label="审批备注"
            value={run.review_note ?? '—'}
          />
          {run.error && (
            <div className="sm:col-span-2">
              <div className="text-xs text-muted-foreground">错误信息</div>
              <div className="mt-1 rounded-md bg-destructive/10 p-2 text-xs text-destructive">
                {run.error}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* 预算与成本 */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <BudgetCard
          icon={<Coins className="h-4 w-4" />}
          title="Token 消耗"
          used={run.total_tokens}
          max={run.max_tokens}
          format={(n) => n.toLocaleString()}
        />
        <BudgetCard
          icon={<DollarSign className="h-4 w-4" />}
          title="成本 (USD)"
          used={run.total_cost_usd}
          max={run.max_cost_usd}
          format={(n) => `$${n.toFixed(4)}`}
        />
        <BudgetCard
          icon={<Clock className="h-4 w-4" />}
          title="输入 Token"
          used={run.input_tokens}
          max={null}
          format={(n) => n.toLocaleString()}
        />
        <BudgetCard
          icon={<Clock className="h-4 w-4" />}
          title="输出 Token"
          used={run.output_tokens}
          max={null}
          format={(n) => n.toLocaleString()}
        />
      </div>

      {/* Planner 执行计划 */}
      {run.plan && (
        <Card>
          <CardContent className="p-4">
            <h3 className="mb-3 font-semibold">课堂处理计划</h3>
            <div className="grid gap-3 text-sm sm:grid-cols-2">
              <PlanItem label="课堂类型" value={run.plan.meeting_type} />
              <PlanItem label="转写策略" value={run.plan.transcript_strategy} />
              <PlanItem
                label="预计 Token"
                value={run.plan.estimated_tokens.toLocaleString()}
              />
              <PlanItem
                label="人工审批"
                value={run.plan.needs_human_review ? '是' : '否'}
              />
              <PlanItem
                label="执行节点"
                value={formatPlanNodes(run.plan)}
              />
              <PlanItem label="原因" value={run.plan.reason} />
            </div>
          </CardContent>
        </Card>
      )}

      {/* 步骤时间线 */}
      <Card>
        <CardContent className="p-4">
          <h3 className="mb-3 font-semibold">
            步骤时间线 ({run.steps.length})
          </h3>
          {run.steps.length === 0 ? (
            <p className="text-sm text-muted-foreground">暂无步骤记录</p>
          ) : (
            <div className="space-y-3">
              {run.steps.map((step, i) => (
                <StepRow key={i} step={step} isLast={i === run.steps.length - 1} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 节点 Token 分布 */}
      {run.node_usage && Object.keys(run.node_usage).length > 0 && (
        <Card>
          <CardContent className="p-4">
            <h3 className="mb-3 font-semibold">节点 Token 分布</h3>
            <div className="space-y-2">
              {Object.entries(run.node_usage).map(([node, usage]) => (
                <div key={node} className="flex items-center justify-between text-sm">
                  <span className="font-mono">{node}</span>
                  <span className="text-muted-foreground">
                    {usage.tokens.toLocaleString()} tokens · ${usage.cost.toFixed(4)}
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Tool 调用记录 */}
      {run.tool_calls.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <h3 className="mb-3 font-semibold">
              Tool 调用记录 ({run.tool_calls.length})
            </h3>
            <div className="space-y-2">
              {run.tool_calls.map((call, i) => (
                <ToolCallRow key={i} call={call} />
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* 审批对话框 */}
      <ReviewDialog run={run} open={reviewOpen} onClose={() => setReviewOpen(false)} />
    </div>
  )
}

// ── 子组件 ──

function InfoItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm font-medium">{value}</div>
    </div>
  )
}

function BudgetCard({
  icon,
  title,
  used,
  max,
  format,
}: {
  icon: React.ReactNode
  title: string
  used: number
  max: number | null
  format: (n: number) => string
}) {
  const pct = max ? Math.min(100, (used / max) * 100) : 0
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between">
          <span className="text-sm text-muted-foreground">{title}</span>
          {icon}
        </div>
        <div className="mt-2 text-xl font-bold">
          {format(used)}
          {max !== null && (
            <span className="ml-1 text-xs font-normal text-muted-foreground">
              / {format(max)}
            </span>
          )}
        </div>
        {max !== null && (
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
            <div
              className={`h-full ${pct > 80 ? 'bg-destructive' : 'bg-primary'}`}
              style={{ width: `${pct}%` }}
            />
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function PlanItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm">{value}</div>
    </div>
  )
}

function formatPlanNodes(plan: {
  should_run_summary: boolean
  should_run_actions: boolean
  should_run_risks: boolean
}): string {
  return [
    plan.should_run_summary && 'summary',
    plan.should_run_actions && 'action_items',
    plan.should_run_risks && 'risks',
  ]
    .filter(Boolean)
    .join(' / ')
}

function StepRow({ step, isLast }: { step: AgentRunStep; isLast: boolean }) {
  const style = STEP_STATUS_STYLES[step.status] ?? {
    dot: 'bg-gray-400',
    badge: 'bg-gray-100 text-gray-700',
  }
  const label = STEP_STATUS_LABELS[step.status] ?? step.status
  return (
    <div className="flex gap-3">
      {/* 时间线圆点 + 连接线 */}
      <div className="flex flex-col items-center">
        <div className={`mt-1 h-3 w-3 rounded-full ${style.dot}`} />
        {!isLast && <div className="w-px flex-1 bg-border" />}
      </div>
      {/* 内容 */}
      <div className="min-w-0 flex-1 pb-3">
        <div className="flex items-center justify-between gap-2">
          <span className="font-mono text-sm font-medium">{step.node}</span>
          <span className={`rounded-md px-1.5 py-0.5 text-xs ${style.badge}`}>
            {label}
          </span>
        </div>
        <div className="mt-0.5 text-xs text-muted-foreground">
          {step.started_at && formatDateTime(step.started_at)}
          {step.finished_at && ` → ${formatDateTime(step.finished_at)}`}
          {step.duration_ms !== undefined && ` · ${step.duration_ms}ms`}
        </div>
        {step.error && (
          <div className="mt-1 rounded-md bg-destructive/10 p-2 text-xs text-destructive">
            {step.error}
          </div>
        )}
      </div>
    </div>
  )
}

function ToolCallRow({ call }: { call: ToolCall }) {
  const [open, setOpen] = useState(false)
  const hasDetail = call.args || call.result || call.error
  return (
    <div className="rounded-md border bg-muted/30 text-sm">
      <button
        type="button"
        disabled={!hasDetail}
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 disabled:cursor-default"
      >
        <div className="flex items-center gap-2">
          {hasDetail ? (
            open ? (
              <ChevronDown className="h-3 w-3 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-3 w-3 text-muted-foreground" />
            )
          ) : (
            <span className="w-3" />
          )}
          <span
            className={`h-2 w-2 rounded-full ${
              call.status === 'succeeded' ? 'bg-green-500' : 'bg-red-500'
            }`}
          />
          <span className="font-mono">{call.tool}</span>
        </div>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span>{call.duration_ms}ms</span>
          <span>{formatDateTime(call.timestamp)}</span>
        </div>
      </button>
      {open && hasDetail && (
        <div className="space-y-2 border-t px-3 py-2 text-xs">
          {call.args && Object.keys(call.args).length > 0 && (
            <div>
              <div className="font-medium text-muted-foreground">args</div>
              <pre className="mt-1 overflow-x-auto rounded bg-background p-2 font-mono">
                {JSON.stringify(call.args, null, 2)}
              </pre>
            </div>
          )}
          {call.result && (
            <div>
              <div className="font-medium text-muted-foreground">result</div>
              <pre className="mt-1 overflow-x-auto rounded bg-background p-2 font-mono">
                {JSON.stringify(call.result, null, 2)}
              </pre>
            </div>
          )}
          {call.error && (
            <div>
              <div className="font-medium text-muted-foreground">error</div>
              <pre className="mt-1 overflow-x-auto rounded bg-destructive/10 p-2 font-mono text-destructive">
                {call.error}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
