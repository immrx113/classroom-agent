import { useParams, useNavigate, useLocation } from 'react-router-dom'
import {
  ArrowLeft,
  GitBranch,
  CheckCircle2,
  Circle,
  Users,
  Clock,
  Link2,
  Quote,
  AlertTriangle,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { useDecisionDetail } from '../hooks/use-decisions'
import { formatDateTime } from '@/lib/utils'
import { cn } from '@/lib/utils'
import type { DecisionOption } from '@/types'

export default function DecisionDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const { data: decision, isLoading } = useDecisionDetail(id)

  const handleBack = () => {
    // 如果有保存的搜索状态，带着状态返回
    const state = location.state as { searchKey?: string } | null
    if (state?.searchKey) {
      navigate('/decisions', { state })
    } else {
      navigate('/decisions')
    }
  }

  if (isLoading) {
    return (
      <div className="space-y-6">
        <Button variant="ghost" onClick={handleBack} className="-ml-2">
          <ArrowLeft className="h-4 w-4" />
          返回列表
        </Button>
        <Skeleton className="h-16" />
        <Skeleton className="h-64" />
      </div>
    )
  }

  if (!decision) {
    return (
      <div className="space-y-6">
        <Button variant="ghost" onClick={handleBack} className="-ml-2">
          <ArrowLeft className="h-4 w-4" />
          返回列表
        </Button>
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <GitBranch className="h-12 w-12 opacity-30" />
          <p className="mt-3 text-sm font-medium">课堂讨论记录不存在或已被删除</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* 返回按钮 */}
      <Button variant="ghost" onClick={handleBack} className="-ml-2">
        <ArrowLeft className="h-4 w-4" />
        返回列表
      </Button>

      {/* 标题区 */}
      <div className="space-y-2">
        <div className="flex items-start justify-between gap-3">
          <h1 className="text-2xl font-bold leading-tight">{decision.title}</h1>
          {decision.confidence != null && (
            <Badge variant="secondary" className="shrink-0">
              置信度 {(decision.confidence * 100).toFixed(0)}%
            </Badge>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-4 text-sm text-muted-foreground">
          {decision.decided_by && decision.decided_by.length > 0 && (
            <span className="flex items-center gap-1">
              <Users className="h-4 w-4" />
              {decision.decided_by.join('、')}
            </span>
          )}
          {decision.decided_at && (
            <span className="flex items-center gap-1">
              <Clock className="h-4 w-4" />
              {formatDateTime(decision.decided_at)}
            </span>
          )}
          {decision.chosen_option && (
            <span className="flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4 text-green-500" />
              讨论结论：{decision.chosen_option}
            </span>
          )}
        </div>
      </div>

      {/* 上下文 */}
      {decision.context && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">讨论背景</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">{decision.context}</p>
          </CardContent>
        </Card>
      )}

      {/* 候选方案 */}
      {decision.options.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">讨论思路</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {decision.options.map((opt) => (
              <OptionItem key={opt.id} option={opt} />
            ))}
          </CardContent>
        </Card>
      )}

      {/* 反对意见 */}
      {decision.objections && decision.objections.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <AlertTriangle className="h-4 w-4 text-amber-500" />
              不同观点与补充意见
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {decision.objections.map((obj, i) => (
                <div
                  key={i}
                  className="rounded-md border-l-4 border-l-amber-500 bg-amber-50 p-3 dark:bg-amber-950/30"
                >
                  <div className="mb-1 flex items-center gap-2">
                    <span className="text-xs font-semibold text-amber-700 dark:text-amber-400">
                      {obj.from}
                    </span>
                    <span className="text-xs text-muted-foreground">不同观点</span>
                  </div>
                  <p className="text-sm text-muted-foreground">{obj.content}</p>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* 选择理由 */}
      {decision.reasons && decision.reasons.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">选择理由</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="list-inside list-disc space-y-1 text-sm text-muted-foreground">
              {decision.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}

      {/* 原文片段 */}
      {decision.snippet && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Quote className="h-4 w-4" />
              原文片段
            </CardTitle>
          </CardHeader>
          <CardContent>
            <blockquote className="border-l-2 pl-4 text-sm italic text-muted-foreground">
              {decision.snippet}
            </blockquote>
          </CardContent>
        </Card>
      )}

      {/* 关联决策 */}
      {decision.related_decisions.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Link2 className="h-4 w-4" />
              相关课堂讨论
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {decision.related_decisions.map((rel) => (
              <div
                key={rel.id}
                className="flex cursor-pointer items-center justify-between rounded-md border p-3 transition-colors hover:bg-accent"
                onClick={() =>
                  navigate(`/decisions/${rel.id}`, {
                    state: location.state, // 保持搜索状态
                  })
                }
              >
                <div className="min-w-0 flex-1">
                  <p className="line-clamp-1 text-sm font-medium">{rel.title}</p>
                  {rel.context && (
                    <p className="line-clamp-1 text-xs text-muted-foreground">
                      {rel.context}
                    </p>
                  )}
                </div>
                {rel.similarity_score != null && (
                  <Badge variant="outline" className="ml-2 shrink-0">
                    {(rel.similarity_score * 100).toFixed(0)}%
                  </Badge>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function OptionItem({ option }: { option: DecisionOption }) {
  return (
    <div
      className={cn(
        'rounded-md border p-3',
        option.is_chosen && 'border-green-500/50 bg-green-50 dark:bg-green-950/30',
      )}
    >
      <div className="flex items-center gap-2">
        {option.is_chosen ? (
          <CheckCircle2 className="h-4 w-4 text-green-500" />
        ) : (
          <Circle className="h-4 w-4 text-muted-foreground" />
        )}
        <span className="text-sm font-medium">{option.name}</span>
        {option.proposed_by && (
          <span className="text-xs text-muted-foreground">· 由 {option.proposed_by} 提出</span>
        )}
      </div>
      {(option.pros && option.pros.length > 0) || (option.cons && option.cons.length > 0) ? (
        <div className="mt-2 grid gap-2 pl-6 text-xs sm:grid-cols-2">
          {option.pros && option.pros.length > 0 && (
            <div>
              <p className="font-medium text-green-600 dark:text-green-400">优点</p>
              <ul className="mt-1 list-inside list-disc text-muted-foreground">
                {option.pros.map((p, i) => (
                  <li key={i}>{p}</li>
                ))}
              </ul>
            </div>
          )}
          {option.cons && option.cons.length > 0 && (
            <div>
              <p className="font-medium text-red-600 dark:text-red-400">缺点</p>
              <ul className="mt-1 list-inside list-disc text-muted-foreground">
                {option.cons.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ) : null}
    </div>
  )
}
