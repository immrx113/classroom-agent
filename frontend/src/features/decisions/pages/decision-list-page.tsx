import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams, useLocation } from 'react-router-dom'
import { GitBranch, Search, Loader2, Filter } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import Pagination from '@/components/ui/pagination'
import { useDecisions, useSearchDecisions } from '../hooks/use-decisions'
import { formatDateTime } from '@/lib/utils'
import type { DecisionListItem, DecisionSearchResult } from '@/types'

export default function DecisionListPage() {
  const [query, setQuery] = useState('')
  const [searchKey, setSearchKey] = useState('')
  const [page, setPage] = useState(0)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const location = useLocation()
  const pageSize = 20
  const meetingIdFromUrl = searchParams.get('meetingId')

  // 从 location.state 恢复搜索状态（从详情页返回时）
  useEffect(() => {
    const state = location.state as { searchKey?: string; page?: number } | null
    if (state?.searchKey) {
      setSearchKey(state.searchKey)
      setQuery(state.searchKey)
      setPage(state.page || 0)
    }
  }, [location.state])

  const { data: listData, isLoading } = useDecisions(
    page * pageSize,
    pageSize,
    meetingIdFromUrl || undefined,
  )
  const { data: searchData, isLoading: isSearching } = useSearchDecisions(
    searchKey,
    searchKey.trim().length > 0,
  )

  const isSearchMode = searchKey.trim().length > 0
  const items = listData?.items || []
  const searchItems = searchData?.items || []

  const handleSearch = () => {
    setSearchKey(query.trim())
    setPage(0) // 搜索时重置页码
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') handleSearch()
  }

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <div>
        <h1 className="text-2xl font-bold">课堂讨论</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          查看课堂讨论中的观点、思路与结论
        </p>
      </div>

      {/* 搜索框 */}
      <div className="flex gap-2">
        <Input
          placeholder="搜索课堂讨论（如：这道题采用了哪种解法）"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          className="max-w-md"
          disabled={!!meetingIdFromUrl}
        />
        <Button onClick={handleSearch} disabled={isSearching || !!meetingIdFromUrl}>
          {isSearching ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Search className="h-4 w-4" />
          )}
          搜索
        </Button>
        {isSearchMode && (
          <Button
            variant="ghost"
            onClick={() => {
              setQuery('')
              setSearchKey('')
              setPage(0)
            }}
          >
            清除
          </Button>
        )}
      </div>

      {/* 按会议筛选提示 */}
      {meetingIdFromUrl && (
        <div className="flex items-center gap-2 rounded-md bg-muted px-4 py-2">
          <Filter className="h-4 w-4" />
          <span className="text-sm">
            正在显示该课时的讨论记录{' '}
            <Button
              variant="link"
              className="h-auto p-0 text-sm"
              onClick={() => navigate('/decisions')}
            >
              查看所有课堂讨论
            </Button>
          </span>
        </div>
      )}

      {/* 内容区 */}
      {isSearchMode ? (
        // 搜索结果
        isSearching ? (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {[...Array(6)].map((_, i) => (
              <Skeleton key={i} className="h-36" />
            ))}
          </div>
        ) : searchItems.length > 0 ? (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              找到 {searchItems.length} 条相关讨论记录
            </p>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {searchItems.map((item) => (
              <SearchResultCard
                key={item.id}
                item={item}
                onClick={() =>
                  navigate(`/decisions/${item.id}`, {
                    state: { searchKey, page },
                  })
                }
              />
              ))}
            </div>
          </div>
        ) : (
          <EmptyState
            icon={<Search className="h-12 w-12 opacity-30" />}
            title="未找到匹配的课堂讨论"
            description="尝试用更通用的关键词重新搜索"
          />
        )
      ) : // 默认列表
      isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[...Array(6)].map((_, i) => (
            <Skeleton key={i} className="h-36" />
          ))}
        </div>
      ) : items.length > 0 ? (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {items.map((item) => (
              <DecisionCard
                key={item.id}
                item={item}
                onClick={() =>
                  navigate(`/decisions/${item.id}`, {
                    state: { searchKey, page },
                  })
                }
              />
            ))}
          </div>
          {listData?.total && listData.total > pageSize && (
            <Pagination
              currentPage={page}
              totalCount={listData.total}
              pageSize={pageSize}
              onPageChange={setPage}
            />
          )}
        </>
      ) : (
        <EmptyState
          icon={<GitBranch className="h-12 w-12 opacity-30" />}
          title="暂无课堂讨论记录"
          description="这里展示从课堂内容中整理的讨论记录"
          action={
            <Button variant="outline" onClick={() => navigate('/')} className="mt-4">
              前往课时列表
            </Button>
          }
        />
      )}
    </div>
  )
}

interface DecisionCardProps {
  item: DecisionListItem
  onClick: () => void
}

function DecisionCard({ item, onClick }: DecisionCardProps) {
  return (
    <Card
      className="cursor-pointer transition-shadow hover:shadow-md"
      onClick={onClick}
    >
      <CardContent className="p-5">
        <div className="mb-2 flex items-start justify-between gap-2">
          <h3 className="line-clamp-2 text-sm font-semibold">{item.title}</h3>
          {item.confidence != null && (
            <Badge variant="secondary" className="shrink-0">
              {(item.confidence * 100).toFixed(0)}%
            </Badge>
          )}
        </div>
        {item.chosen_option && (
          <p className="line-clamp-2 text-sm text-muted-foreground">
            <span className="font-medium text-foreground">讨论结论：</span>
            {item.chosen_option}
          </p>
        )}
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          {item.decided_by && item.decided_by.length > 0 && (
            <span className="truncate">
              {item.decided_by.slice(0, 2).join('、')}
              {item.decided_by.length > 2 && '等'}
            </span>
          )}
          <span>·</span>
          <span>{formatDateTime(item.created_at)}</span>
        </div>
      </CardContent>
    </Card>
  )
}

interface SearchResultCardProps {
  item: DecisionSearchResult
  onClick: () => void
}

function SearchResultCard({ item, onClick }: SearchResultCardProps) {
  return (
    <Card
      className="cursor-pointer transition-shadow hover:shadow-md"
      onClick={onClick}
    >
      <CardContent className="p-5">
        <div className="mb-2 flex items-start justify-between gap-2">
          <h3 className="line-clamp-2 text-sm font-semibold">{item.title}</h3>
          <Badge variant="outline" className="shrink-0">
            {item.score.toFixed(2)}
          </Badge>
        </div>
        {item.context && (
          <p className="line-clamp-2 text-sm text-muted-foreground">{item.context}</p>
        )}
        {item.chosen_option && (
          <p className="mt-1 line-clamp-1 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">讨论结论：</span>
            {item.chosen_option}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: React.ReactNode
  title: string
  description: string
  action?: React.ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
      {icon}
      <p className="mt-3 text-sm font-medium">{title}</p>
      <p className="mt-1 text-xs">{description}</p>
      {action}
    </div>
  )
}
