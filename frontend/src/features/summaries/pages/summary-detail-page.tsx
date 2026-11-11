import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  ArrowLeft,
  FileText,
  CheckSquare,
  AlertTriangle,
  Sparkles,
  Loader2,
  AlertCircle,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { MarkdownRenderer } from '@/components/ui/markdown-renderer'
import { ActionItemList } from '../components/action-item-list'
import { RiskList } from '../components/risk-list'
import {
  useMeetingSummary,
  useGenerateSummary,
  useUpdateActionItem,
} from '../hooks/use-summaries'
import { useMeeting } from '@/features/meetings/hooks/use-meetings'
import { cn } from '@/lib/utils'
import type { ActionItem } from '@/types'

type Tab = 'summary' | 'actions' | 'risks'

export default function SummaryListPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState<Tab>('summary')

  const { data: meeting } = useMeeting(id)
  const { data: summaryData, isLoading } = useMeetingSummary(id)
  const generateSummary = useGenerateSummary()
  const updateActionItem = useUpdateActionItem(id)

  const summary = summaryData?.summary
  const actionItems = summaryData?.action_items || []
  const risks = summaryData?.risks || []

  const isGenerating = generateSummary.isPending
  const summaryStatus = summary?.status

  const handleGenerate = async () => {
    if (!id) return
    try {
      await generateSummary.mutateAsync(id)
    } catch (err) {
      console.error('生成失败:', err)
    }
  }

  const handleToggleActionStatus = (item: ActionItem) => {
    const newStatus = item.status === 'done' ? 'pending' : 'done'
    updateActionItem.mutate({ itemId: item.id, data: { status: newStatus } })
  }

  return (
    <div className="space-y-6">
      {/* 返回按钮 */}
      <Button variant="ghost" onClick={() => navigate('/summaries')} className="-ml-2">
        <ArrowLeft className="h-4 w-4" />
        返回列表
      </Button>
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold">
            {meeting?.title || '课堂摘要'}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            AI 整理的课堂内容摘要与学习任务
          </p>
        </div>
        <Button
          onClick={handleGenerate}
          disabled={isGenerating}
        >
          {isGenerating ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Sparkles className="h-4 w-4" />
          )}
          {isGenerating ? '生成中...' : '生成课堂摘要'}
        </Button>
      </div>

      {/* 生成中提示 */}
      {summaryStatus === 'generating' && (
        <Card className="border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950">
          <CardContent className="flex items-center gap-3 p-4">
            <Loader2 className="h-5 w-5 animate-spin text-blue-500" />
            <div>
              <p className="text-sm font-medium text-blue-900 dark:text-blue-100">
                正在整理课堂摘要...
              </p>
              <p className="text-xs text-blue-700 dark:text-blue-300">
                正在整理课堂内容、学习任务与注意事项
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 生成失败提示 */}
      {summaryStatus === 'failed' && (
        <Card className="border-destructive/50 bg-destructive/5">
          <CardContent className="flex items-center gap-3 p-4">
            <AlertCircle className="h-5 w-5 text-destructive" />
            <div>
              <p className="text-sm font-medium">课堂摘要生成失败</p>
              <p className="text-xs text-muted-foreground">请重试或检查转写内容</p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Tab 切换 */}
      <div className="flex gap-1 border-b">
        <TabButton
          active={activeTab === 'summary'}
          onClick={() => setActiveTab('summary')}
          icon={<FileText className="h-4 w-4" />}
          label="课堂摘要"
          count={summary ? 1 : 0}
        />
        <TabButton
          active={activeTab === 'actions'}
          onClick={() => setActiveTab('actions')}
          icon={<CheckSquare className="h-4 w-4" />}
          label="学习任务"
          count={actionItems.length}
        />
        <TabButton
          active={activeTab === 'risks'}
          onClick={() => setActiveTab('risks')}
          icon={<AlertTriangle className="h-4 w-4" />}
          label="注意事项"
          count={risks.length}
        />
      </div>

      {/* 内容区 */}
      {isLoading ? (
        <Skeleton className="h-64" />
      ) : (
        <>
          {activeTab === 'summary' && (
            <Card>
              <CardHeader>
                <CardTitle>课堂摘要</CardTitle>
              </CardHeader>
              <CardContent>
                {summaryStatus === 'generating' ? (
                  <div className="flex items-center justify-center py-12">
                    <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                  </div>
                ) : summary?.content ? (
                  <>
                    <MarkdownRenderer content={summary.content} />
                    {summary.key_points && summary.key_points.length > 0 && (
                      <div className="mt-6 border-t pt-4">
                        <h4 className="mb-2 text-sm font-semibold">关键要点</h4>
                        <MarkdownRenderer
                          content={summary.key_points.map((p) => `- ${p}`).join('\n')}
                          className="text-sm text-muted-foreground"
                        />
                      </div>
                    )}
                  </>
                ) : (
                  <EmptyState
                    icon={<FileText className="h-8 w-8" />}
                    title="尚未生成课堂摘要"
                    description="点击右上角「生成课堂摘要」按钮开始"
                  />
                )}
              </CardContent>
            </Card>
          )}

          {activeTab === 'actions' && (
            <ActionItemList items={actionItems} onToggleStatus={handleToggleActionStatus} />
          )}

          {activeTab === 'risks' && <RiskList risks={risks} />}
        </>
      )}
    </div>
  )
}

function TabButton({
  active,
  onClick,
  icon,
  label,
  count,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
  count: number
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'flex items-center gap-2 border-b-2 px-4 py-2 text-sm font-medium transition-colors',
        active
          ? 'border-primary text-primary'
          : 'border-transparent text-muted-foreground hover:text-foreground',
      )}
    >
      {icon}
      {label}
      {count > 0 && (
        <span className="rounded-full bg-muted px-1.5 py-0.5 text-xs">
          {count}
        </span>
      )}
    </button>
  )
}

function EmptyState({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode
  title: string
  description: string
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
      <div className="opacity-50">{icon}</div>
      <p className="mt-3 text-sm font-medium">{title}</p>
      <p className="mt-1 text-xs">{description}</p>
    </div>
  )
}
