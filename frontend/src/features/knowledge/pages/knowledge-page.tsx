import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Search,
  Upload,
  FileText,
  Trash2,
  Loader2,
  Database,
  Calendar,
  ExternalLink,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Dialog } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { HighlightText } from '@/components/ui/highlight-text'
import {
  useKnowledgeDocuments,
  useDeleteKnowledgeDocument,
  useKnowledgeSearch,
  useUploadDocument,
} from '../hooks/use-knowledge'
import { formatDateTime } from '@/lib/utils'
import type { SearchResult, KnowledgeDocument } from '@/types'

export default function KnowledgePage() {
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState<'search' | 'documents'>('search')
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SearchResult[]>([])
  const [uploadProgress, setUploadProgress] = useState(0)
  // 上传阶段：idle | uploading | indexing | done
  const [uploadStage, setUploadStage] = useState<'idle' | 'uploading' | 'indexing'>('idle')
  const [actionMsg, setActionMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; title: string } | null>(null)

  const { data: documents, isLoading: docsLoading } = useKnowledgeDocuments()
  const deleteDoc = useDeleteKnowledgeDocument()
  const search = useKnowledgeSearch()
  const upload = useUploadDocument()

  const handleSearch = async () => {
    if (!searchQuery.trim()) return
    setActionMsg(null)
    try {
      const response = await search.mutateAsync({ query: searchQuery })
      setSearchResults(response.results)
      if (response.results.length === 0) {
        setActionMsg({ type: 'error', text: '未找到相关结果' })
      }
    } catch (err) {
      console.error('检索失败:', err)
      setActionMsg({ type: 'error', text: err instanceof Error ? err.message : '检索失败，请稍后重试' })
    }
  }

  const handleDeleteClick = (docId: string, docTitle: string) => {
    setDeleteTarget({ id: docId, title: docTitle })
  }

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return
    setActionMsg(null)
    try {
      await deleteDoc.mutateAsync(deleteTarget.id)
      setActionMsg({ type: 'success', text: `「${deleteTarget.title}」已删除` })
    } catch (err) {
      console.error('删除失败:', err)
      setActionMsg({ type: 'error', text: err instanceof Error ? err.message : '删除失败' })
    }
    setDeleteTarget(null)
  }

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setActionMsg(null)
    setUploadStage('uploading')
    setUploadProgress(0)
    try {
      // 阶段1：网络上传（XHR progress 事件驱动进度条）
      await upload.mutateAsync({
        file,
        onProgress: (p) => {
          setUploadProgress(p)
          // 网络上传完成后进入索引阶段
          if (p >= 100) {
            setUploadStage('indexing')
          }
        },
      })
      // 阶段2：后端索引完成（mutateAsync resolve 即索引完成）
      setUploadProgress(0)
      setUploadStage('idle')
      setActionMsg({ type: 'success', text: `「${file.name}」上传并索引成功` })
    } catch (err) {
      console.error('上传失败:', err)
      setUploadProgress(0)
      setUploadStage('idle')
      setActionMsg({ type: 'error', text: err instanceof Error ? err.message : '上传失败' })
    }
    e.target.value = ''
  }

  return (
    <div className="space-y-6">
      {/* 标题栏 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">知识库</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            企业知识管理与智能检索
          </p>
        </div>
        <label className="inline-flex h-9 cursor-pointer items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-50">
          <input
            type="file"
            className="hidden"
            onChange={handleUpload}
            accept=".pdf,.docx,.doc,.txt,.md"
            disabled={upload.isPending}
          />
          {upload.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Upload className="h-4 w-4" />
          )}
          {upload.isPending
            ? uploadStage === 'indexing'
              ? '索引中...（解析/分块/向量化）'
              : `上传中 ${uploadProgress}%`
            : '上传文档'}
        </label>
      </div>

      {/* 操作反馈消息 */}
      {actionMsg && (
        <div
          className={`flex items-center gap-2 rounded-md px-4 py-2 text-sm ${
            actionMsg.type === 'success'
              ? 'bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-400'
              : 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-400'
          }`}
        >
          <span>{actionMsg.type === 'success' ? '✓' : '✗'}</span>
          <span>{actionMsg.text}</span>
          <button
            onClick={() => setActionMsg(null)}
            className="ml-auto text-xs opacity-60 hover:opacity-100"
          >
            关闭
          </button>
        </div>
      )}

      {/* Tab 切换 */}
      <div className="flex gap-1 border-b">
        <button
          onClick={() => setActiveTab('search')}
          className={`flex items-center gap-2 border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
            activeTab === 'search'
              ? 'border-primary text-primary'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          }`}
        >
          <Search className="h-4 w-4" />
          知识检索
        </button>
        <button
          onClick={() => setActiveTab('documents')}
          className={`flex items-center gap-2 border-b-2 px-4 py-2 text-sm font-medium transition-colors ${
            activeTab === 'documents'
              ? 'border-primary text-primary'
              : 'border-transparent text-muted-foreground hover:text-foreground'
          }`}
        >
          <Database className="h-4 w-4" />
          文档管理
        </button>
      </div>

      {/* 检索界面 */}
      {activeTab === 'search' && (
        <div className="space-y-4">
          {/* 搜索框 */}
          <div className="flex gap-2">
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              placeholder="输入关键词搜索知识库..."
              className="flex-1"
            />
            <Button onClick={handleSearch} disabled={search.isPending || !searchQuery.trim()}>
              {search.isPending ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Search className="h-4 w-4" />
              )}
              搜索
            </Button>
          </div>

          {/* 检索结果 */}
          {search.isPending ? (
            <div className="space-y-3">
              {[...Array(3)].map((_, i) => (
                <Skeleton key={i} className="h-24" />
              ))}
            </div>
          ) : searchResults.length > 0 ? (
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground">
                找到 {searchResults.length} 条相关结果
              </p>
              {searchResults.map((result) => (
                <SearchResultCard
                  key={result.id}
                  result={result}
                  keywords={searchQuery}
                  onNavigate={navigate}
                />
              ))}
            </div>
          ) : search.isIdle ? (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Search className="h-12 w-12 opacity-30" />
              <p className="mt-3 text-sm">输入关键词开始检索</p>
              <p className="mt-1 text-xs">支持课堂摘要与学习文档</p>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Search className="h-12 w-12 opacity-30" />
              <p className="mt-3 text-sm">未找到相关结果</p>
            </div>
          )}
        </div>
      )}

      {/* 文档管理 */}
      {activeTab === 'documents' && (
        <div className="space-y-3">
          {docsLoading ? (
            <div className="space-y-3">
              {[...Array(5)].map((_, i) => (
                <Skeleton key={i} className="h-16" />
              ))}
            </div>
          ) : documents && documents.length > 0 ? (
            documents.map((doc) => (
              <DocumentCard
                key={doc.id}
                doc={doc}
                onDelete={() => handleDeleteClick(doc.id, doc.title)}
              />
            ))
          ) : (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Database className="h-12 w-12 opacity-30" />
              <p className="mt-3 text-sm">知识库为空</p>
              <p className="mt-1 text-xs">上传学习文档或生成课堂摘要后自动入库</p>
            </div>
          )}
        </div>
      )}

      {/* 删除确认对话框 */}
      <Dialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title="删除文档"
        description={`确定删除「${deleteTarget?.title}」吗？该文档的所有分块将被永久删除，此操作不可撤销。`}
        closeOnOverlayClick
      >
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="outline" onClick={() => setDeleteTarget(null)}>
            取消
          </Button>
          <Button
            variant="destructive"
            onClick={handleDeleteConfirm}
            disabled={deleteDoc.isPending}
          >
            {deleteDoc.isPending ? '删除中...' : '删除'}
          </Button>
        </div>
      </Dialog>
    </div>
  )
}

function SearchResultCard({
  result,
  keywords,
  onNavigate,
}: {
  result: SearchResult
  keywords: string
  onNavigate: (path: string) => void
}) {
  const sourceTypeLabel =
    result.source_type === 'meeting_summary' ? '课堂摘要' : '学习文档'

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-2 flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            <FileText className="h-4 w-4 text-muted-foreground" />
            <h3 className="text-sm font-medium">{result.title}</h3>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant="info">{sourceTypeLabel}</Badge>
            {result.rerank_score !== undefined && result.rerank_score > 0 && (
              <Badge variant="secondary">
                相关度 {(result.rerank_score * 100).toFixed(0)}%
              </Badge>
            )}
          </div>
        </div>

        <p className="line-clamp-3 text-sm text-muted-foreground">
          <HighlightText text={result.content} keywords={keywords} />
        </p>

        {result.source_id && (
          <button
            onClick={() => onNavigate(`/summaries/${result.source_id}`)}
            className="mt-2 inline-flex items-center gap-1 text-xs text-primary hover:underline"
          >
            <ExternalLink className="h-3 w-3" />
            查看来源
          </button>
        )}
      </CardContent>
    </Card>
  )
}

function DocumentCard({
  doc,
  onDelete,
}: {
  doc: KnowledgeDocument
  onDelete: () => void
}) {
  const sourceTypeLabel =
    doc.source_type === 'meeting_summary' ? '课堂摘要' : '学习文档'

  return (
    <Card>
      <CardContent className="flex items-center justify-between p-4">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
            <FileText className="h-5 w-5 text-primary" />
          </div>
          <div>
            <p className="text-sm font-medium">{doc.title}</p>
            <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
              <Badge variant="secondary">{sourceTypeLabel}</Badge>
              <span className="flex items-center gap-1">
                <Calendar className="h-3 w-3" />
                {formatDateTime(doc.created_at)}
              </span>
            </div>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={onDelete}
          className="text-muted-foreground hover:text-destructive"
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      </CardContent>
    </Card>
  )
}
