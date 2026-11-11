import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Activity,
  DollarSign,
  Coins,
  CheckCircle2,
  XCircle,
  PauseCircle,
  PlayCircle,
  Shield,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  AgentRunStatusBadge,
  ReviewStatusBadge,
} from '../components/agent-run-status-badge'
import { ReviewDialog } from '../components/review-dialog'
import { useAgentRuns, useAgentRunStats, useTools } from '../hooks/use-agent-runs'
import { formatDateTime } from '@/lib/utils'
import type { AgentRun, AgentRunStatus } from '@/types'

const STATUS_FILTERS: { label: string; value: AgentRunStatus | 'all' }[] = [
  { label: '全部', value: 'all' },
  { label: '执行中', value: 'running' },
  { label: '待审批', value: 'paused' },
  { label: '成功', value: 'succeeded' },
  { label: '失败', value: 'failed' },
]

export default function AgentRunListPage() {
  const [statusFilter, setStatusFilter] = useState<AgentRunStatus | 'all'>('all')
  const [reviewRun, setReviewRun] = useState<AgentRun | null>(null)
  const navigate = useNavigate()

  const { data: stats, isLoading: statsLoading } = useAgentRunStats()
  const { data, isLoading } = useAgentRuns({
    status: statusFilter === 'all' ? undefined : statusFilter,
    page_size: 50,
  })
  const { data: toolsData } = useTools()

  const runs = data?.items ?? []

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <div>
        <h1 className="text-2xl font-bold">课堂处理记录</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Harness 工作流全链路可观测：预算 / 步骤 / 审批 / 工具
        </p>
      </div>

      {/* 统计卡片 */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatCard
          title="总运行数"
          value={stats?.total_runs ?? 0}
          icon={<Activity className="h-4 w-4" />}
          loading={statsLoading}
        />
        <StatCard
          title="成功率"
          value={`${((stats?.success_rate ?? 0) * 100).toFixed(1)}%`}
          icon={<CheckCircle2 className="h-4 w-4" />}
          loading={statsLoading}
        />
        <StatCard
          title="总 Token"
          value={(stats?.total_tokens ?? 0).toLocaleString()}
          icon={<Coins className="h-4 w-4" />}
          loading={statsLoading}
        />
        <StatCard
          title="总成本 (USD)"
          value={`$${(stats?.total_cost_usd ?? 0).toFixed(4)}`}
          icon={<DollarSign className="h-4 w-4" />}
          loading={statsLoading}
        />
      </div>

      {/* 状态分布 */}
      {stats && (
        <div className="flex flex-wrap gap-3 text-sm">
          {(['running', 'paused', 'succeeded', 'failed'] as AgentRunStatus[]).map((s) => (
            <div
              key={s}
              className="flex items-center gap-1.5 rounded-md border bg-card px-3 py-1.5"
            >
              <StatusIcon status={s} />
              <span className="text-muted-foreground">{statusLabel(s)}</span>
              <span className="font-semibold">
                {stats.status_counts[s] ?? 0}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* 状态过滤 */}
      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((f) => (
          <Button
            key={f.value}
            size="sm"
            variant={statusFilter === f.value ? 'default' : 'outline'}
            onClick={() => setStatusFilter(f.value)}
          >
            {f.label}
          </Button>
        ))}
      </div>

      {/* Run 列表 */}
      {isLoading ? (
        <div className="space-y-3">
          {[...Array(5)].map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : runs.length > 0 ? (
        <div className="space-y-3">
          {runs.map((run) => (
            <RunRow
              key={run.id}
              run={run}
              onClick={() => navigate(`/agent-runs/${run.id}`)}
              onReview={() => setReviewRun(run)}
            />
          ))}
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16">
          <Activity className="h-10 w-10 text-muted-foreground/50" />
          <p className="mt-3 text-sm text-muted-foreground">暂无课堂处理记录</p>
        </div>
      )}

      {/* Tool Registry 概览 */}
      {toolsData && toolsData.tools.length > 0 && (
        <Card>
          <CardContent className="p-4">
            <div className="mb-3 flex items-center gap-2">
              <Shield className="h-4 w-4" />
              <h3 className="font-medium">Tool Registry ({toolsData.tools.length})</h3>
            </div>
            <div className="flex flex-wrap gap-2">
              {toolsData.tools.map((t) => (
                <div
                  key={t.name}
                  className="rounded-md border bg-muted/30 px-2.5 py-1 text-xs"
                  title={t.description}
                >
                  <span className="font-mono">{t.name}</span>
                  <span className="ml-2 text-muted-foreground">
                    {riskLabel(t.risk)}
                  </span>
                  {t.requires_confirmation && (
                    <span className="ml-1 text-yellow-600">⚠</span>
                  )}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* 审批对话框 */}
      <ReviewDialog
        run={reviewRun}
        open={!!reviewRun}
        onClose={() => setReviewRun(null)}
      />
    </div>
  )
}

// ── 子组件 ──

interface StatCardProps {
  title: string
  value: string | number
  icon: React.ReactNode
  loading?: boolean
}

function StatCard({ title, value, icon, loading }: StatCardProps) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center justify-between">
          <span className="text-sm text-muted-foreground">{title}</span>
          {icon}
        </div>
        <div className="mt-2 text-2xl font-bold">
          {loading ? <Skeleton className="h-7 w-16" /> : value}
        </div>
      </CardContent>
    </Card>
  )
}

function RunRow({
  run,
  onClick,
  onReview,
}: {
  run: AgentRun
  onClick: () => void
  onReview: () => void
}) {
  const stepCount = run.steps.length
  const succeededSteps = run.steps.filter((s) => s.status === 'succeeded').length
  const failedSteps = run.steps.filter(
    (s) => s.status === 'failed' || s.status === 'timeout' || s.status === 'budget_exceeded',
  ).length
  const budgetPct = run.max_tokens
    ? Math.min(100, (run.total_tokens / run.max_tokens) * 100)
    : 0

  return (
    <Card className="cursor-pointer transition-shadow hover:shadow-md" onClick={onClick}>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-3">
          {/* 左侧 */}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="flex items-center gap-2">
              <AgentRunStatusBadge status={run.status} />
              {run.review_status && <ReviewStatusBadge status={run.review_status} />}
              <span className="font-mono text-xs text-muted-foreground">
                {run.id.slice(0, 8)}
              </span>
              <span className="text-xs text-muted-foreground">·</span>
              <span className="text-xs text-muted-foreground">
                meeting {run.meeting_id.slice(0, 8)}
              </span>
            </div>
            <div className="text-xs text-muted-foreground">
              {formatDateTime(run.started_at)}
              {run.finished_at && ` → ${formatDateTime(run.finished_at)}`}
            </div>
            <div className="text-xs text-muted-foreground">
              步骤: {succeededSteps}/{stepCount} 成功
              {failedSteps > 0 && (
                <span className="text-destructive"> · {failedSteps} 失败</span>
              )}
            </div>
          </div>

          {/* 右侧：预算 + 成本 */}
          <div className="shrink-0 space-y-1 text-right">
            <div className="text-xs text-muted-foreground">
              Token: {run.total_tokens.toLocaleString()} / {run.max_tokens.toLocaleString()}
            </div>
            <div className="h-1.5 w-32 overflow-hidden rounded-full bg-muted">
              <div
                className={`h-full ${budgetPct > 80 ? 'bg-destructive' : 'bg-primary'}`}
                style={{ width: `${budgetPct}%` }}
              />
            </div>
            <div className="text-xs text-muted-foreground">
              成本: ${run.total_cost_usd.toFixed(4)} / ${run.max_cost_usd.toFixed(2)}
            </div>
          </div>

          {/* 审批按钮 */}
          {run.status === 'paused' && run.review_status === 'pending' && (
            <Button
              size="sm"
              variant="default"
              onClick={(e) => {
                e.stopPropagation()
                onReview()
              }}
            >
              审批
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

function StatusIcon({ status }: { status: AgentRunStatus }) {
  const cls = 'h-3.5 w-3.5'
  switch (status) {
    case 'running':
      return <PlayCircle className={`${cls} text-blue-500`} />
    case 'paused':
      return <PauseCircle className={`${cls} text-yellow-500`} />
    case 'succeeded':
      return <CheckCircle2 className={`${cls} text-green-500`} />
    case 'failed':
      return <XCircle className={`${cls} text-red-500`} />
    default:
      return <Activity className={`${cls} text-muted-foreground`} />
  }
}

function statusLabel(s: AgentRunStatus): string {
  const m: Record<AgentRunStatus, string> = {
    pending: '待执行',
    running: '执行中',
    succeeded: '成功',
    failed: '失败',
    paused: '待审批',
    cancelled: '已取消',
  }
  return m[s]
}

function riskLabel(risk: string): string {
  const m: Record<string, string> = {
    read_only: '读',
    write_safe: '写·安全',
    write_danger: '写·危险',
    system: '系统',
  }
  return m[risk] ?? risk
}
