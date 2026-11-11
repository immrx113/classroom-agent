import { useEffect, useRef, useState } from 'react'
import {
  Search,
  Upload,
  FileText,
  Trash2,
  Loader2,
  Database,
  Play,
  ExternalLink,
  FileAudio,
  Clock,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { listCourses, listLectures } from '@/api/courses'
import {
  searchRetrieval,
  listCourseDocuments,
  uploadCourseDocument,
  deleteDocument,
  getDocumentBlocks,
} from '../api'
import type {
  Course,
  CourseDocument,
  LectureBrief,
  ParsedBlock,
  RetrievalResult,
  RetrievalSearchResponse,
} from '@/types'

function sourceTypeLabel(t: string): string {
  if (t === 'transcript') return '转写'
  if (t === 'document') return '课件'
  if (t === 'note') return '笔记'
  return t
}

export default function RetrievalPage() {
  const [courses, setCourses] = useState<Course[]>([])
  const [courseId, setCourseId] = useState<string>('')
  const [lectures, setLectures] = useState<LectureBrief[]>([])
  const [lectureId, setLectureId] = useState<string>('')
  const [query, setQuery] = useState('')
  const [response, setResponse] = useState<RetrievalSearchResponse | null>(null)
  const [searching, setSearching] = useState(false)
  const [docs, setDocs] = useState<CourseDocument[]>([])
  const [docsLoading, setDocsLoading] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<'search' | 'documents'>('search')

  // 音频回听：按证据固定资产定位播放，源切换后等元数据就绪再 seek。
  const audioRef = useRef<HTMLAudioElement>(null)
  const pendingSeekMsRef = useRef<number | null>(null)
  const [mediaSrc, setMediaSrc] = useState<string | null>(null)
  const [openDoc, setOpenDoc] = useState<{ blocks: ParsedBlock[]; blockId?: string } | null>(null)
  const [openDocLoading, setOpenDocLoading] = useState(false)

  const loadCourses = async () => {
    try {
      const cs = await listCourses()
      setCourses(cs)
    } catch (e) {
      console.error('加载课程失败:', e)
    }
  }

  const loadLectures = async (cid: string) => {
    if (!cid) {
      setLectures([])
      return
    }
    try {
      setLectures(await listLectures(cid))
    } catch (e) {
      console.error('加载课时失败:', e)
      setLectures([])
    }
  }

  const loadDocs = async (cid: string) => {
    if (!cid) {
      setDocs([])
      return
    }
    setDocsLoading(true)
    try {
      setDocs(await listCourseDocuments(cid))
    } catch (e) {
      console.error('加载文档失败:', e)
      setDocs([])
    } finally {
      setDocsLoading(false)
    }
  }

  useEffect(() => {
    void loadCourses()
  }, [])

  // 切换课程：清空课时/结果/文档，避免 C1 旧结果串入 C2（不引入缓存）。
  const handleCourseChange = (cid: string) => {
    setCourseId(cid)
    setLectureId('')
    setResponse(null)
    setDocs([])
    setOpenDoc(null)
    setMediaSrc(null)
    void loadLectures(cid)
    void loadDocs(cid)
  }

  const handleSearch = async () => {
    if (!query.trim()) return
    setError(null)
    setSearching(true)
    setOpenDoc(null)
    try {
      const res = await searchRetrieval({
        query: query.trim(),
        courseId: courseId || undefined,
        lectureId: lectureId || undefined,
      })
      setResponse(res)
    } catch (e) {
      console.error('检索失败:', e)
      setError(e instanceof Error ? e.message : '检索失败')
    } finally {
      setSearching(false)
    }
  }

  const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file || !courseId) return
    setError(null)
    setUploading(true)
    try {
      await uploadCourseDocument(courseId, file, lectureId || undefined)
      await loadDocs(courseId)
    } catch (err) {
      console.error('上传失败:', err)
      setError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setUploading(false)
      e.target.value = ''
    }
  }

  const handleDelete = async (id: string) => {
    setError(null)
    try {
      await deleteDocument(id)
      await loadDocs(courseId)
    } catch (err) {
      console.error('删除失败:', err)
      setError(err instanceof Error ? err.message : '删除失败')
    }
  }

  const handleOpenDocument = async (r: RetrievalResult) => {
    if (!r.source_document_id) return
    setOpenDocLoading(true)
    try {
      const blocks = await getDocumentBlocks(r.source_document_id)
      setOpenDoc({ blocks, blockId: r.block_id ?? undefined })
    } catch (e) {
      console.error('读取文档块失败:', e)
      setOpenDoc({ blocks: [] })
    } finally {
      setOpenDocLoading(false)
    }
  }

  const playAt = (targetSrc: string, startMs: number) => {
    if (targetSrc === mediaSrc) {
      if (audioRef.current) {
        audioRef.current.currentTime = startMs / 1000
        void audioRef.current.play()
      }
      return
    }
    pendingSeekMsRef.current = startMs
    setMediaSrc(targetSrc)
  }

  const handleLoadedMetadata = () => {
    const startMs = pendingSeekMsRef.current
    if (startMs != null && audioRef.current) {
      pendingSeekMsRef.current = null
      audioRef.current.currentTime = startMs / 1000
      void audioRef.current.play()
    }
  }

  const handleOpenTranscript = (r: RetrievalResult) => {
    if (r.start_ms == null) return // 缺时间：只定位原文，不回听
    const targetSrc = r.source_audio_asset_id
      ? `/api/audio-assets/${r.source_audio_asset_id}/content`
      : undefined
    if (targetSrc) playAt(targetSrc, r.start_ms)
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">课堂检索</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            在课程/课时范围内检索转写、课件与笔记，并打开真实来源
          </p>
        </div>
        <label className="inline-flex h-9 cursor-pointer items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-50">
          <input
            type="file"
            className="hidden"
            onChange={handleUpload}
            accept=".pdf,.docx,.txt,.md,.markdown"
            disabled={uploading || !courseId}
          />
          {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
          {uploading ? '上传中...' : '上传课件'}
        </label>
      </div>

      {/* 范围选择 */}
      <div className="flex flex-wrap items-end gap-3 rounded-md border p-3">
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted-foreground">课程范围</label>
          <select
            className="h-9 min-w-48 rounded-md border bg-background px-2 text-sm"
            value={courseId}
            onChange={(e) => handleCourseChange(e.target.value)}
          >
            <option value="">全部课程</option>
            {courses.map((c) => (
              <option key={c.id} value={c.id}>
                {c.title}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-muted-foreground">课时范围（可选）</label>
          <select
            className="h-9 min-w-48 rounded-md border bg-background px-2 text-sm"
            value={lectureId}
            onChange={(e) => setLectureId(e.target.value)}
            disabled={!courseId}
          >
            <option value="">整门课程</option>
            {lectures.map((l) => (
              <option key={l.id} value={l.id}>
                {l.title}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-400">
          ✗ {error}
        </div>
      )}

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
          检索
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

      {activeTab === 'search' && (
        <div className="space-y-4">
          <div className="flex gap-2">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              placeholder="输入关键词检索（课程/课时范围内）..."
              className="flex-1"
            />
            <Button onClick={handleSearch} disabled={searching || !query.trim()}>
              {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
              搜索
            </Button>
          </div>

          {response && (
            <p className="text-sm text-muted-foreground">
              状态 {response.status}
              {response.index_status === 'none' ? ' · 无索引' : ''} · 共 {response.total} 条结果
            </p>
          )}

          {searching ? (
            <div className="space-y-3">
              {[...Array(3)].map((_, i) => (
                <Skeleton key={i} className="h-24" />
              ))}
            </div>
          ) : response && response.results.length > 0 ? (
            <div className="space-y-3">
              {response.results.map((r, i) => (
                <ResultCard
                  key={i}
                  result={r}
                  onOpenTranscript={() => handleOpenTranscript(r)}
                  onOpenDocument={() => handleOpenDocument(r)}
                  onOpenEvidence={
                    r.note_evidence_ids?.length
                      ? () => {
                          // 笔记结果打开其原始证据（回听来源）
                          setMediaSrc(null)
                          setOpenDoc(null)
                        }
                      : undefined
                  }
                />
              ))}
            </div>
          ) : response ? (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Search className="h-12 w-12 opacity-30" />
              <p className="mt-3 text-sm">未找到相关结果</p>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Search className="h-12 w-12 opacity-30" />
              <p className="mt-3 text-sm">选择范围并输入关键词开始检索</p>
            </div>
          )}

          {/* 回听音频（转写来源，按固定资产定位） */}
          {mediaSrc && (
            <Card>
              <CardContent className="p-4">
                <audio
                  ref={audioRef}
                  controls
                  className="w-full"
                  data-testid="retrieval-audio"
                  src={mediaSrc}
                  onLoadedMetadata={handleLoadedMetadata}
                >
                  您的浏览器不支持音频播放
                </audio>
              </CardContent>
            </Card>
          )}

          {/* 文档原文块（课件来源，真实定位） */}
          {openDoc && (
            <Card>
              <CardContent className="space-y-2 p-4">
                {openDocLoading ? (
                  <Skeleton className="h-20" />
                ) : (
                  openDoc.blocks
                    .filter((b) => !openDoc.blockId || b.block_id === openDoc.blockId)
                    .map((b) => (
                      <div key={b.block_id} className="rounded-md border p-3 text-sm">
                        <div className="mb-1 flex flex-wrap items-center gap-2">
                          <Badge variant="secondary">{b.kind}</Badge>
                          {b.page_no != null && (
                            <Badge variant="info">第 {b.page_no} 页</Badge>
                          )}
                          {b.heading && <span className="text-xs text-muted-foreground">{b.heading}</span>}
                        </div>
                        <p className="whitespace-pre-wrap text-muted-foreground">{b.text}</p>
                      </div>
                    ))
                )}
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {activeTab === 'documents' && (
        <div className="space-y-3">
          {docsLoading ? (
            <div className="space-y-3">
              {[...Array(4)].map((_, i) => (
                <Skeleton key={i} className="h-16" />
              ))}
            </div>
          ) : docs.length > 0 ? (
            docs.map((doc) => (
              <Card key={doc.id}>
                <CardContent className="flex items-center justify-between p-4">
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                      <FileText className="h-5 w-5 text-primary" />
                    </div>
                    <div>
                      <p className="text-sm font-medium">{doc.title}</p>
                      <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                        <Badge variant="secondary">{doc.filename || doc.title}</Badge>
                        {doc.latest_version && (
                          <Badge variant="outline">
                            {doc.latest_version.parse_status} · {doc.latest_version.block_count} 块
                          </Badge>
                        )}
                      </div>
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => handleDelete(doc.id)}
                    className="text-muted-foreground hover:text-destructive"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </CardContent>
              </Card>
            ))
          ) : courseId ? (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Database className="h-12 w-12 opacity-30" />
              <p className="mt-3 text-sm">该课程暂无课件</p>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
              <Database className="h-12 w-12 opacity-30" />
              <p className="mt-3 text-sm">请先选择课程</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ResultCard({
  result,
  onOpenTranscript,
  onOpenDocument,
}: {
  result: RetrievalResult
  onOpenTranscript: () => void
  onOpenDocument: () => void
  onOpenEvidence?: () => void
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-2 flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            {result.source_type === 'transcript' ? (
              <FileAudio className="h-4 w-4 text-muted-foreground" />
            ) : (
              <FileText className="h-4 w-4 text-muted-foreground" />
            )}
            <Badge variant="info">{sourceTypeLabel(result.source_type)}</Badge>
            {!result.is_current && <Badge variant="warning">旧版本</Badge>}
          </div>
          <div className="flex items-center gap-2">
            {result.start_ms != null && result.end_ms != null && (
              <Badge variant="outline">
                <Clock className="mr-1 h-3 w-3" />
                {(result.start_ms / 1000).toFixed(1)}s–{(result.end_ms / 1000).toFixed(1)}s
              </Badge>
            )}
            {result.page_no != null && <Badge variant="outline">第 {result.page_no} 页</Badge>}
          </div>
        </div>

        <p className="line-clamp-3 text-sm text-muted-foreground">
          {result.content?.join('\n') || result.quote}
        </p>

        <div className="mt-2 flex items-center gap-2">
          {result.source_type === 'transcript' && result.start_ms != null && (
            <Button variant="outline" size="sm" onClick={onOpenTranscript}>
              <Play className="h-3 w-3" />
              回听来源
            </Button>
          )}
          {result.source_type === 'document' && (
            <Button variant="outline" size="sm" onClick={onOpenDocument}>
              <ExternalLink className="h-3 w-3" />
              打开原文
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
