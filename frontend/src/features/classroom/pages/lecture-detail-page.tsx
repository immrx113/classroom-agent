import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Loader2,
  Upload,
  FileText,
  Play,
  Clock,
  FileAudio,
  RotateCcw,
  Sparkles,
  Send,
} from 'lucide-react'
import { AudioUploader } from '@/features/meetings/components/audio-uploader'
import {
  getLecture,
  listTranscriptSegments,
  uploadLectureAudio,
  getJob,
  getLatestJob,
  retryJob,
  uploadLectureDocument,
  listCourseDocuments,
  getDocumentBlocks,
  organizeLecture,
  listNotes,
  publishNote,
} from '../api'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import type {
  CourseDocument,
  Job,
  Lecture,
  Note,
  ParsedBlock,
  TranscriptSegment,
} from '@/types'

const TERMINAL = ['succeeded', 'failed', 'cancelled']

function fmtMs(ms?: number | null): string {
  if (ms == null) return '—'
  return `${(ms / 1000).toFixed(1)}s`
}

function jobStatusLabel(job: Job | null): string {
  if (!job) return '未开始'
  if (job.status_display) return job.status_display
  return job.status
}

function errorLabel(job: Job | null): string | null {
  if (!job?.error_type) return null
  const map: Record<string, string> = {
    provider_error: '供应商调用失败',
    timeout: '供应商超时',
    rate_limited: '供应商限流',
    budget_exhausted: '预算不足',
    provider_unknown: '调用结果待核对',
    no_speech: '未识别到语音',
    invalid_result: '转写结果校验失败',
  }
  return map[job.error_type] ?? job.error_type
}

function noteStatusLabel(status?: string): string {
  if (status === 'draft') return '草稿（待确认）'
  if (status === 'pending') return '待确认'
  if (status === 'published') return '已发布'
  return status ?? '—'
}

// 笔记正文预览（仅 confirmed/非空条目，与后端正式知识口径对齐）。
function notePreview(note: Note): string {
  const c = note.content
  if (!c) return ''
  const parts: string[] = []
  if (c.title) parts.push(c.title)
  for (const s of c.sections ?? []) {
    if (s.heading) parts.push(s.heading)
    for (const p of s.points ?? []) {
      if (p.text) parts.push(`· ${p.text}`)
    }
  }
  for (const kc of c.key_conclusions ?? []) {
    if (kc.text) parts.push(`· ${kc.text}`)
  }
  for (const kp of c.knowledge_points ?? []) {
    if (kp.definition) parts.push(`· 定义：${kp.definition}`)
    if (kp.applicability) parts.push(`· 适用：${kp.applicability}`)
  }
  for (const ex of c.examples ?? []) {
    if (ex.problem) parts.push(`· 例题：${ex.problem}`)
    if (ex.derivation) parts.push(`· 解：${ex.derivation}`)
  }
  for (const pt of c.key_points ?? []) {
    if (pt.point) parts.push(`· 难点：${pt.point}`)
    if (pt.reason) parts.push(`· 依据：${pt.reason}`)
  }
  return parts.join('\n')
}

// 选「待确认草稿」优先，否则取最新一条（后端按 created_at desc 返回）。
function pickActiveNote(notes: Note[]): Note | null {
  if (notes.length === 0) return null
  const draft = notes.find((n) => n.status === 'draft' || n.status === 'pending')
  if (draft) return draft
  return notes[0] ?? null
}

export default function LectureDetailPage() {
  const { courseId = '', lectureId = '' } = useParams()
  const [lecture, setLecture] = useState<Lecture | null>(null)
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [docs, setDocs] = useState<CourseDocument[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // 音频上传 + 任务
  const [audioFile, setAudioFile] = useState<File | null>(null)
  const [uploading, setUploading] = useState(false)
  const [job, setJob] = useState<Job | null>(null)
  const [jobPolling, setJobPolling] = useState(false)
  const pollStopRef = useRef(false)

  // 课件
  const [docUploading, setDocUploading] = useState(false)

  // 全课整理 / 草稿确认 / 发布
  const [organizeJob, setOrganizeJob] = useState<Job | null>(null)
  const [organizing, setOrganizing] = useState(false)
  const [note, setNote] = useState<Note | null>(null)
  const [publishing, setPublishing] = useState(false)

  // 音频回听 + 文档原文定位
  const audioRef = useRef<HTMLAudioElement>(null)
  const pendingSeekMsRef = useRef<number | null>(null)
  const [openDoc, setOpenDoc] = useState<{ docId: string; blocks: ParsedBlock[] } | null>(null)
  const [openDocLoading, setOpenDocLoading] = useState(false)

  const loadAll = useCallback(async () => {
    setLoading(true)
    try {
      const [lec, segs, ds, latestJob, ns] = await Promise.all([
        getLecture(lectureId),
        listTranscriptSegments(lectureId),
        listCourseDocuments(courseId),
        getLatestJob(lectureId),
        listNotes(lectureId),
      ])
      setLecture(lec)
      setSegments(segs)
      setDocs(ds)
      if (latestJob) setJob(latestJob)
      setNote(pickActiveNote(ns))
    } catch (e) {
      console.error('加载课时失败:', e)
      setError(e instanceof Error ? e.message : '加载课时失败')
    } finally {
      setLoading(false)
    }
  }, [courseId, lectureId])

  useEffect(() => {
    void loadAll()
    return () => {
      pollStopRef.current = true
    }
  }, [loadAll])

  const pollJob = useCallback(
    async (jobId: string) => {
      pollStopRef.current = false
      setJobPolling(true)
      try {
        while (!pollStopRef.current) {
          const j = await getJob(jobId)
          setJob(j)
          if (TERMINAL.includes(j.status)) break
          await new Promise((r) => setTimeout(r, 2000))
        }
      } catch (e) {
        console.error('查询任务失败:', e)
      } finally {
        setJobPolling(false)
        await loadAll()
      }
    },
    [loadAll],
  )

  const handleAudioSelect = (file: File | null) => {
    setAudioFile(file)
  }

  const handleUploadAudio = async () => {
    if (!audioFile) return
    setError(null)
    setUploading(true)
    try {
      const res = await uploadLectureAudio(lectureId, audioFile)
      setAudioFile(null)
      await pollJob(res.job_id)
    } catch (e) {
      console.error('上传音频失败:', e)
      setError(e instanceof Error ? e.message : '上传音频失败')
    } finally {
      setUploading(false)
    }
  }

  const handleRetry = async () => {
    if (!job) return
    setError(null)
    try {
      const j = await retryJob(job.id)
      await pollJob(j.id)
    } catch (e) {
      console.error('重试失败:', e)
      setError(e instanceof Error ? e.message : '重试失败')
    }
  }

  const refreshNote = useCallback(async () => {
    try {
      setNote(pickActiveNote(await listNotes(lectureId)))
    } catch (e) {
      console.error('加载笔记失败:', e)
    }
  }, [lectureId])

  const handleOrganize = async () => {
    const tvId = lecture?.active_transcript?.id
    if (!tvId) return
    setError(null)
    setOrganizing(true)
    try {
      const created = await organizeLecture(lectureId, tvId)
      setOrganizeJob(created)
      // 轮询整理任务直至终态（后台 worker 派发整理任务）
      pollStopRef.current = false
      setJobPolling(true)
      try {
        while (!pollStopRef.current) {
          const j = await getJob(created.id)
          setOrganizeJob(j)
          if (TERMINAL.includes(j.status)) break
          await new Promise((r) => setTimeout(r, 1500))
        }
      } finally {
        setJobPolling(false)
      }
      await refreshNote()
    } catch (e) {
      console.error('整理失败:', e)
      setError(e instanceof Error ? e.message : '整理失败')
    } finally {
      setOrganizing(false)
    }
  }

  const handlePublish = async () => {
    if (!note || (note.status !== 'draft' && note.status !== 'pending')) return
    setError(null)
    setPublishing(true)
    try {
      await publishNote(note.id)
      await refreshNote()
      await loadAll()
    } catch (e) {
      console.error('发布失败:', e)
      setError(e instanceof Error ? e.message : '发布失败')
    } finally {
      setPublishing(false)
    }
  }

  const handleDocUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setError(null)
    setDocUploading(true)
    try {
      await uploadLectureDocument(courseId, lectureId, file)
      const ds = await listCourseDocuments(courseId)
      setDocs(ds)
    } catch (err) {
      console.error('上传课件失败:', err)
      setError(err instanceof Error ? err.message : '上传课件失败')
    } finally {
      setDocUploading(false)
      e.target.value = ''
    }
  }

  const handleOpenDoc = async (docId: string) => {
    setOpenDocLoading(true)
    try {
      const blocks = await getDocumentBlocks(docId)
      setOpenDoc({ docId, blocks })
    } catch (e) {
      console.error('读取文档块失败:', e)
      setOpenDoc({ docId, blocks: [] })
    } finally {
      setOpenDocLoading(false)
    }
  }

  const playAt = (startMs: number) => {
    const audio = audioRef.current
    if (audio && audio.readyState >= 1) {
      // 播放器已就绪：直接 seek 到对应原句（修复首次点选无响应）
      audio.currentTime = startMs / 1000
      // 快速连续点选会打断上一次 play()，这里吞掉中断拒绝，避免未处理 Promise 拒绝
      audio.play().catch(() => {})
    } else {
      // 尚未就绪：暂存目标毫秒，待 loadedmetadata 后应用
      pendingSeekMsRef.current = startMs
    }
  }

  const handleLoadedMetadata = () => {
    const startMs = pendingSeekMsRef.current
    if (startMs != null && audioRef.current) {
      pendingSeekMsRef.current = null
      audioRef.current.currentTime = startMs / 1000
      audioRef.current.play().catch(() => {})
    }
  }

  const activeAudioSrc =
    lecture?.active_audio?.id
      ? `/api/audio-assets/${lecture.active_audio.id}/content`
      : null

  // 课件归属：只展示「本课时绑定」+「课程共享（lecture_id 为空）」课件，兄弟课时课件不可见
  const scopedDocs = docs.filter((doc) => doc.lecture_id == null || doc.lecture_id === lectureId)

  return (
    <div className="space-y-6">
      <div>
        <Link
          to={`/courses/${courseId}`}
          className="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" />
          返回课时列表
        </Link>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{lecture?.title ?? '课时'}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {lecture?.description || '上传录音与课件，转写后可点选片段回听'}
            </p>
          </div>
          {lecture && <Badge variant="secondary">{lecture.status}</Badge>}
        </div>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-400">
          ✗ {error}
        </div>
      )}

      {/* 音频上传 */}
      <Card>
        <CardContent className="space-y-4 p-4">
          <h2 className="text-sm font-semibold">录音上传与转写</h2>
          <AudioUploader
            onFileSelect={handleAudioSelect}
            selectedFile={audioFile}
            isUploading={uploading}
          />
          {audioFile && (
            <Button onClick={handleUploadAudio} disabled={uploading}>
              {uploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
              上传并转写
            </Button>
          )}

          {job && (
            <div className="flex flex-wrap items-center gap-2 rounded-md border p-3 text-sm">
              <Badge data-testid="job-status" variant={job.status === 'succeeded' ? 'success' : job.status === 'failed' ? 'destructive' : 'secondary'}>
                {jobStatusLabel(job)}
              </Badge>
              {job.error_type && (
                <span data-testid="job-error" className="text-muted-foreground">
                  {errorLabel(job)}{job.error_code ? `（${job.error_code}）` : ''}
                </span>
              )}
              {job.stage && <span className="text-muted-foreground">阶段 {job.stage}</span>}
              {jobPolling && <Loader2 className="h-4 w-4 animate-spin" />}
              {(job.status === 'failed' || job.status === 'retry_wait') && job.retryable && (
                <Button variant="outline" size="sm" data-testid="job-retry" onClick={handleRetry}>
                  <RotateCcw className="h-3 w-3" />
                  重试
                </Button>
              )}
            </div>
          )}

          {activeAudioSrc && (
            <audio
              ref={audioRef}
              controls
              className="w-full"
              data-testid="lecture-audio"
              src={activeAudioSrc}
              onLoadedMetadata={handleLoadedMetadata}
            >
              您的浏览器不支持音频播放
            </audio>
          )}
        </CardContent>
      </Card>

      {/* 转写段（点选回听） */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <h2 className="text-sm font-semibold">
            转写片段{lecture?.active_transcript ? ` · ${lecture.active_transcript.segment_count} 段` : ''}
          </h2>
          {loading ? (
            <Skeleton className="h-20" />
          ) : segments.length > 0 ? (
            <div className="space-y-2">
              {segments.map((s) => (
                <button
                  key={s.id}
                  data-testid="transcript-segment"
                  disabled={s.start_ms == null || !activeAudioSrc}
                  onClick={() => {
                    if (activeAudioSrc && s.start_ms != null) playAt(s.start_ms)
                  }}
                  className="flex w-full items-start gap-3 rounded-md border p-3 text-left text-sm transition-colors hover:bg-accent disabled:cursor-default disabled:hover:bg-transparent"
                >
                  <div className="flex items-center gap-2">
                    {s.start_ms != null ? (
                      <Play className="h-4 w-4 shrink-0 text-primary" />
                    ) : (
                      <FileAudio className="h-4 w-4 shrink-0 text-muted-foreground" />
                    )}
                  </div>
                  <div className="flex-1">
                    <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                      <Badge variant="outline">
                        <Clock className="mr-1 h-3 w-3" />
                        {fmtMs(s.start_ms)} – {fmtMs(s.end_ms)}
                      </Badge>
                      {s.speaker && <span>{s.speaker}</span>}
                    </div>
                    <p className="text-muted-foreground">{s.content}</p>
                  </div>
                </button>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              {lecture?.active_transcript ? '暂无转写片段' : '尚未转写，请先上传录音'}
            </p>
          )}
        </CardContent>
      </Card>

      {/* 课件上传 + 原文定位 */}
      <Card>
        <CardContent className="space-y-4 p-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">课件</h2>
            <label className="inline-flex h-9 cursor-pointer items-center justify-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow transition-colors hover:bg-primary/90 disabled:pointer-events-none disabled:opacity-50">
              <input
                type="file"
                className="hidden"
                onChange={handleDocUpload}
                accept=".pdf,.docx,.txt,.md,.markdown"
                disabled={docUploading}
              />
              {docUploading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
              上传课件
            </label>
          </div>

          {scopedDocs.length > 0 ? (
            <div className="space-y-2">
              {scopedDocs.map((doc) => (
                <div
                  key={doc.id}
                  data-testid="course-doc"
                  data-lecture-id={doc.lecture_id ?? ''}
                  data-shared={doc.lecture_id == null ? 'true' : 'false'}
                  className="flex items-center justify-between rounded-md border p-3"
                >
                  <div className="flex items-center gap-3">
                    <FileText className="h-4 w-4 text-muted-foreground" />
                    <div>
                      <p className="text-sm font-medium">{doc.title}</p>
                      <div className="mt-0.5 flex items-center gap-2">
                        <Badge variant={doc.lecture_id == null ? 'secondary' : 'outline'}>
                          {doc.lecture_id == null ? '课程共享' : '本课时'}
                        </Badge>
                        {doc.latest_version && (
                          <span className="text-xs text-muted-foreground">
                            {doc.latest_version.parse_status} · {doc.latest_version.block_count} 块
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => handleOpenDoc(doc.id)}>
                    查看原文
                  </Button>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">暂无课件</p>
          )}

          {openDoc && (
            <div className="space-y-2 border-t pt-3">
              {openDocLoading ? (
                <Skeleton className="h-20" />
              ) : openDoc.blocks.length > 0 ? (
                openDoc.blocks.map((b) => (
                  <div key={b.block_id} className="rounded-md border p-3 text-sm">
                    <div className="mb-1 flex flex-wrap items-center gap-2">
                      <Badge variant="secondary">{b.kind}</Badge>
                      {b.page_no != null && <Badge variant="info">第 {b.page_no} 页</Badge>}
                      {b.heading && <span className="text-xs text-muted-foreground">{b.heading}</span>}
                    </div>
                    <p className="whitespace-pre-wrap text-muted-foreground">{b.text}</p>
                  </div>
                ))
              ) : (
                <p className="text-sm text-muted-foreground">该课件暂无解析块</p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 全课整理 / 草稿确认 / 发布 */}
      <Card>
        <CardContent className="space-y-4 p-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">全课整理与发布</h2>
            {note?.status === 'published' && (
              <Link
                to={`/notes/${note.id}`}
                data-testid="note-detail-link"
                className="text-xs text-muted-foreground hover:text-foreground"
              >
                查看笔记详情 →
              </Link>
            )}
          </div>

          {!lecture?.active_transcript ? (
            <p className="text-sm text-muted-foreground">请先上传录音并完成转写，再发起整理。</p>
          ) : (
            <>
              {organizeJob && (organizeJob.status === 'running' || organizeJob.status === 'queued' || organizeJob.status === 'failed') && (
                <div className="flex flex-wrap items-center gap-2 rounded-md border p-3 text-sm">
                  <Badge
                    data-testid="organize-status"
                    variant={organizeJob.status === 'failed' ? 'destructive' : 'secondary'}
                  >
                    {jobStatusLabel(organizeJob)}
                  </Badge>
                  {organizeJob.error_type && (
                    <span className="text-muted-foreground">{errorLabel(organizeJob)}</span>
                  )}
                  {jobPolling && <Loader2 className="h-4 w-4 animate-spin" />}
                </div>
              )}

              {note ? (
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    <Badge
                      data-testid="note-status"
                      variant={note.status === 'published' ? 'success' : 'warning'}
                    >
                      {noteStatusLabel(note.status)}
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      {note.status === 'published'
                        ? '已进入正式知识，可被问答检索'
                        : '草稿尚未发布，不可被问答检索'}
                    </span>
                  </div>
                  <pre
                    data-testid="note-preview"
                    className="whitespace-pre-wrap rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground"
                  >
                    {notePreview(note) || '（无正文）'}
                  </pre>
                  {(note.status === 'draft' || note.status === 'pending') && (
                    <Button
                      data-testid="publish-button"
                      onClick={handlePublish}
                      disabled={publishing || jobPolling}
                    >
                      {publishing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                      确认发布
                    </Button>
                  )}
                </div>
              ) : (
                <Button
                  data-testid="organize-button"
                  onClick={handleOrganize}
                  disabled={organizing || jobPolling}
                >
                  {organizing || jobPolling ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                  整理本课
                </Button>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
