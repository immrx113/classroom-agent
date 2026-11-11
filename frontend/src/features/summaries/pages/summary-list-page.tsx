import { useNavigate } from 'react-router-dom'
import { FileText, Sparkles, Loader2, AlertCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { useSummaries } from '../hooks/use-summaries'
import { formatDateTime } from '@/lib/utils'
import type { SummaryListItem } from '@/types'

export default function SummaryListPage() {
  const { data: summaries, isLoading } = useSummaries()
  const navigate = useNavigate()

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <div>
        <h1 className="text-2xl font-bold">课堂摘要</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          查看 AI 整理的课堂内容摘要
        </p>
      </div>

      {/* 纪要列表 */}
      {isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[...Array(6)].map((_, i) => (
            <Skeleton key={i} className="h-36" />
          ))}
        </div>
      ) : summaries && summaries.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {summaries.map((item) => (
            <SummaryCard
              key={item.id}
              item={item}
              onClick={() => navigate(`/summaries/${item.meeting_id}`)}
            />
          ))}
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <FileText className="h-12 w-12 opacity-30" />
          <p className="mt-3 text-sm font-medium">暂无课堂摘要</p>
          <p className="mt-1 text-xs">在课时详情页点击「生成课堂摘要」按钮即可创建</p>
          <Button variant="outline" onClick={() => navigate('/')} className="mt-4">
            前往课时列表
          </Button>
        </div>
      )}
    </div>
  )
}

interface SummaryCardProps {
  item: SummaryListItem
  onClick: () => void
}

function SummaryCard({ item, onClick }: SummaryCardProps) {
  const statusBadge = (() => {
    switch (item.status) {
      case 'completed':
        return <Badge variant="success">已完成</Badge>
      case 'generating':
        return (
          <Badge variant="info">
            <Loader2 className="mr-1 h-3 w-3 animate-spin" />
            生成中
          </Badge>
        )
      case 'failed':
        return (
          <Badge variant="destructive">
            <AlertCircle className="mr-1 h-3 w-3" />
            失败
          </Badge>
        )
      default:
        return <Badge variant="secondary">{item.status}</Badge>
    }
  })()

  return (
    <Card
      className="cursor-pointer transition-shadow hover:shadow-md"
      onClick={onClick}
    >
      <CardContent className="p-5">
        <div className="mb-2 flex items-start justify-between gap-2">
          <h3 className="line-clamp-1 text-sm font-semibold">
            {item.meeting_title}
          </h3>
          {statusBadge}
        </div>
        <p className="line-clamp-3 text-sm text-muted-foreground">
          {item.content || '暂无内容'}
        </p>
        <div className="mt-3 flex items-center gap-2 text-xs text-muted-foreground">
          <Sparkles className="h-3 w-3" />
          <span>{formatDateTime(item.created_at)}</span>
        </div>
      </CardContent>
    </Card>
  )
}
