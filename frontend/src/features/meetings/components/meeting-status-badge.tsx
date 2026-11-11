import { Badge } from '@/components/ui/badge'
import type { MeetingStatus } from '@/types'

const statusConfig: Record<MeetingStatus, { label: string; variant: 'default' | 'info' | 'success' | 'warning' | 'destructive' }> = {
  pending: { label: '待处理', variant: 'warning' },
  transcribing: { label: '转写中', variant: 'info' },
  processed: { label: '已完成', variant: 'success' },
  failed: { label: '失败', variant: 'destructive' },
}

export function MeetingStatusBadge({ status }: { status: MeetingStatus }) {
  const config = statusConfig[status] || statusConfig.pending
  return <Badge variant={config.variant}>{config.label}</Badge>
}
