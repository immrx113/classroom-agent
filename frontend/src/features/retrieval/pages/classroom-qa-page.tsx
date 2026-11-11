import { useEffect, useRef, useState } from 'react'
import {
  Send,
  Loader2,
  Play,
  FileText,
  FileAudio,
  Clock,
  ExternalLink,
  Bot,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { listCourses, listLectures } from '@/api/courses'
import { getDocumentBlocks } from '../api'
import { getNote } from '@/features/notes/api'
import { streamClassroomChat } from '@/api/chat'
import type {
  Course,
  LectureBrief,
  NoteContent,
  ParsedBlock,
  RetrievalResult,
} from '@/types'

function sourceTypeLabel(t: string): string {
  if (t === 'transcript') return '转写'
  if (t === 'document') return '课件'
  if (t === 'note') return '笔记'
  return t
}

// 与后端 retrieval_service._note_text 保持一致：仅 confirmed 条目进入正式知识文本。
function noteContentText(content?: NoteContent | null): string {
  if (!content) return ''
  const parts: string[] = []
  if (content.title) parts.push(content.title)
  for (const s of content.sections ?? []) {
    if (s.heading) parts.push(s.heading)
    for (const p of s.points ?? []) {
      if (p.text && p.status === 'confirmed') parts.push(p.text)
    }
  }
  for (const kc of content.key_conclusions ?? []) {
    if (kc.text && kc.status === 'confirmed') parts.push(kc.text)
  }
  for (const kp of content.knowledge_points ?? []) {
    if (kp.status !== 'confirmed') continue
    if (kp.definition) parts.push(kp.definition)
    if (kp.applicability) parts.push(kp.applicability)
  }
  for (const ex of content.examples ?? []) {
    if (ex.status !== 'confirmed') continue
    if (ex.problem) parts.push(ex.problem)
    if (ex.derivation) parts.push(ex.derivation)
  }
  for (const pt of content.key_points ?? []) {
    if (pt.status !== 'confirmed') continue
    if (pt.point) parts.push(pt.point)
    if (pt.reason) parts.push(pt.reason)
  }
  return parts.join('\n')
}

interface QaTurn {
  id: string
  query: string
  answer: string
  status?: string
  sources: RetrievalResult[]
  streaming: boolean
  error?: string
}

export default function ClassroomQaPage() {
  const [courses, setCourses] = useState<Course[]>([])
  const [courseId, setCourseId] = useState('')
  const [lectures, setLectures] = useState<LectureBrief[]>([])
  const [lectureId, setLectureId] = useState('')
  const [query, setQuery] = useState('')
  const [turns, setTurns] = useState<QaTurn[]>([])
  const [error, setError] = useState<string | null>(null)
  const [streaming, setStreaming] = useState(false)

  // 音频回听 + 课件原文定位（点击引用跳转，复用资料预览与音频定位）
  const audioRef = useRef<HTMLAudioElement>(null)
  const pendingSeekMsRef = useRef<number | null>(null)
  const [mediaSrc, setMediaSrc] = useState<string | null>(null)
  const [openDoc, setOpenDoc] = useState<{ blocks: ParsedBlock[]; blockId?: string } | null>(null)
  const [openDocLoading, setOpenDocLoading] = useState(false)
  const [openNote, setOpenNote] = useState<{ noteId: string; text: string } | null>(null)
  const [openNoteLoading, setOpenNoteLoading] = useState(false)

  const loadCourses = async () => {
    try {
      setCourses(await listCourses())
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

  useEffect(() => {
    void loadCourses()
  }, [])

  // 切换课程：清空课时/对话/来源，避免旧课程结果串入新课程（不引入缓存）
  const handleCourseChange = (cid: string) => {
    setCourseId(cid)
    setLectureId('')
    setTurns([])
    setOpenDoc(null)
    setOpenNote(null)
    setMediaSrc(null)
    void loadLectures(cid)
  }

  const handleSend = async () => {
    if (!query.trim() || streaming) return
    const q = query.trim()
    setQuery('')
    setError(null)
    setOpenDoc(null)
    setOpenNote(null)
    setStreaming(true)

    const turnId = crypto.randomUUID()
    setTurns((prev) => [
      ...prev,
      { id: turnId, query: q, answer: '', sources: [], streaming: true },
    ])

    const patch = (p: Partial<QaTurn>) =>
      setTurns((prev) => prev.map((t) => (t.id === turnId ? { ...t, ...p } : t)))

    try {
      let acc = ''
      for await (const ev of streamClassroomChat({
        query: q,
        courseId: courseId || undefined,
        lectureId: lectureId || undefined,
      })) {
        if (ev.type === 'sources') {
          patch({ sources: ev.results ?? [], status: ev.status })
        } else if (ev.type === 'token' && ev.content) {
          acc += ev.content
          patch({ answer: acc })
        } else if (ev.type === 'done') {
          patch({ streaming: false, status: ev.status })
        } else if (ev.type === 'error') {
          patch({ error: ev.message ?? '未知错误', streaming: false })
        }
      }
      patch({ streaming: false })
    } catch (e) {
      patch({ error: e instanceof Error ? e.message : '请求失败', streaming: false })
    } finally {
      setStreaming(false)
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
    if (r.start_ms == null) return
    const targetSrc = r.source_audio_asset_id
      ? `/api/audio-assets/${r.source_audio_asset_id}/content`
      : undefined
    if (targetSrc) playAt(targetSrc, r.start_ms)
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

  const handleOpenNote = async (r: RetrievalResult) => {
    // 笔记来源的 source_version_id 即 NoteVersion.id（笔记 id）。
    const noteId = r.source_version_id
    if (!noteId) return
    setOpenNoteLoading(true)
    try {
      const note = await getNote(noteId)
      setOpenNote({ noteId, text: noteContentText(note.content) })
    } catch (e) {
      console.error('读取笔记失败:', e)
      setOpenNote({ noteId, text: '' })
    } finally {
      setOpenNoteLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">课堂问答</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          在课程/课时范围内提问，流式返回答案与可跳转来源（转写 / 课件 / 笔记）
        </p>
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

      {/* 对话记录 */}
      <div className="space-y-4">
        {turns.length === 0 && !streaming ? (
          <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
            <Bot className="h-12 w-12 opacity-30" />
            <p className="mt-3 text-sm">选择范围并提问，例如课件细节、结构化例题</p>
          </div>
        ) : (
          turns.map((turn) => <TurnCard key={turn.id} turn={turn} onOpenTranscript={handleOpenTranscript} onOpenDocument={handleOpenDocument} onOpenNote={handleOpenNote} />)
        )}
      </div>

      {/* 回听音频（转写来源，按固定资产定位） */}
      {mediaSrc && (
        <Card>
          <CardContent className="p-4">
            <audio
              ref={audioRef}
              controls
              className="w-full"
              data-testid="qa-audio"
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
                      {b.page_no != null && <Badge variant="info">第 {b.page_no} 页</Badge>}
                      {b.heading && <span className="text-xs text-muted-foreground">{b.heading}</span>}
                    </div>
                    <p className="whitespace-pre-wrap text-muted-foreground">{b.text}</p>
                  </div>
                ))
            )}
          </CardContent>
        </Card>
      )}

      {/* 笔记原文（笔记来源，正文与引用一致） */}
      {openNote && (
        <Card>
          <CardContent className="space-y-2 p-4">
            <div className="flex items-center gap-2">
              <Badge variant="info">笔记</Badge>
              <span className="text-xs text-muted-foreground">正文（仅 confirmed 条目）</span>
            </div>
            {openNoteLoading ? (
              <Skeleton className="h-20" />
            ) : (
              <p data-testid="qa-note-content" className="whitespace-pre-wrap text-muted-foreground">
                {openNote.text || '（无正文）'}
              </p>
            )}
          </CardContent>
        </Card>
      )}

      {/* 输入框 */}
      <div className="flex gap-2 border-t pt-4">
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && handleSend()}
          placeholder="输入问题，Enter 发送..."
          disabled={streaming}
          className="flex-1"
        />
        <Button onClick={handleSend} disabled={!query.trim() || streaming}>
          {streaming ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
          提问
        </Button>
      </div>
    </div>
  )
}

function TurnCard({
  turn,
  onOpenTranscript,
  onOpenDocument,
  onOpenNote,
}: {
  turn: QaTurn
  onOpenTranscript: (r: RetrievalResult) => void
  onOpenDocument: (r: RetrievalResult) => void
  onOpenNote: (r: RetrievalResult) => void
}) {
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-lg bg-primary px-4 py-2 text-sm text-primary-foreground">
          {turn.query}
        </div>
      </div>
      <div className="space-y-2">
        {turn.streaming && !turn.answer && !turn.error ? (
          <Skeleton className="h-16" />
        ) : (
          <>
            {turn.answer && (
              <div data-testid="qa-answer" className="rounded-lg border bg-muted/40 px-4 py-3 text-sm whitespace-pre-wrap">
                {turn.answer}
              </div>
            )}
            {turn.error && (
              <div className="rounded-md bg-red-50 px-4 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-400">
                ✗ {turn.error}
              </div>
            )}
          </>
        )}
        {turn.sources.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground">来源（点击跳转）：</p>
            {turn.sources.map((r, i) => (
              <SourceCard key={i} result={r} onOpenTranscript={() => onOpenTranscript(r)} onOpenDocument={() => onOpenDocument(r)} onOpenNote={() => onOpenNote(r)} />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function SourceCard({
  result,
  onOpenTranscript,
  onOpenDocument,
  onOpenNote,
}: {
  result: RetrievalResult
  onOpenTranscript: () => void
  onOpenDocument: () => void
  onOpenNote: () => void
}) {
  return (
    <Card>
      <CardContent className="p-3">
        <div className="mb-1 flex items-start justify-between gap-2">
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

        <p data-testid="qa-source-content" className="line-clamp-3 text-sm text-muted-foreground">
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
          {result.source_type === 'note' && (
            <Button variant="outline" size="sm" onClick={onOpenNote}>
              <FileText className="h-3 w-3" />
              打开笔记
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
