import { Badge, type BadgeProps } from '@/components/ui/badge'
import type { AgentRunStatus, ReviewStatus } from '@/types'

interface AgentRunStatusBadgeProps {
  status: AgentRunStatus
}

const STATUS_CONFIG: Record<
  AgentRunStatus,
  { label: string; variant: BadgeProps['variant'] }
> = {
  pending: { label: '待执行', variant: 'secondary' },
  running: { label: '执行中', variant: 'info' },
  succeeded: { label: '成功', variant: 'success' },
  failed: { label: '失败', variant: 'destructive' },
  paused: { label: '待审批', variant: 'warning' },
  cancelled: { label: '已取消', variant: 'secondary' },
}

export function AgentRunStatusBadge({ status }: AgentRunStatusBadgeProps) {
  const config = STATUS_CONFIG[status]
  return <Badge variant={config.variant}>{config.label}</Badge>
}

const REVIEW_CONFIG: Record<
  Exclude<ReviewStatus, null>,
  { label: string; variant: BadgeProps['variant'] }
> = {
  pending: { label: '待审批', variant: 'warning' },
  approved: { label: '已批准', variant: 'success' },
  rejected: { label: '已拒绝', variant: 'destructive' },
  skipped: { label: '免审批', variant: 'secondary' },
}

export function ReviewStatusBadge({ status }: { status: ReviewStatus }) {
  if (!status) return null
  const config = REVIEW_CONFIG[status]
  return <Badge variant={config.variant}>{config.label}</Badge>
}
