import { useRef, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import {
  ArrowLeft,
  FileAudio,
  FileText,
  Clock,
  Play,
  Quote,
  CheckCircle2,
  HelpCircle,
  ListChecks,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { useNote } from '../hooks/use-notes'
import { getEvidence } from '../api'
import type { NoteConclusion, NoteEvidence } from '@/types'

// 状态标签
function statusBadge(status?: string) {
  if (status === 'pending') return <Badge variant="warning">待确认</Badge>
  if (status === 'confirmed') return <Badge variant="success">已核对</Badge>
  return null
}

function originLabel(origin?: string) {
  if (origin === 'teacher_explicit') return '教师明确'
  if (origin === 'material_derived') return '材料推导'
  if (origin === 'model_supplement') return '模型补充'
  return '来源'
}

export default function NoteDetailPage() {
  const { noteId } = useParams<{ noteId: string }>()
  const navigate = useNavigate()
  const { data: note, isLoading } = useNote(noteId)
  const audioRef = useRef<HTMLAudioElement>(null)
  const [evidence, setEvidence] = useState<NoteEvidence | null>(null)
  const [evidenceLoading, setEvidenceLoading] = useState(false)
  // 回听媒体源：默认课时当前音频；点击引用后切到证据固定资产，保证旧引用回听旧来源。
  const [mediaSrc, setMediaSrc] = useState<string | null>(null)
  // 待定位时间（ms）：源切换后等 loadedmetadata 再 seek，避免元数据未加载就 set currentTime。
  const pendingSeekMsRef = useRef<number | null>(null)

  const defaultSrc = note?.lecture_id ? `/api/meetings/${note.lecture_id}/audio` : null
  const src = mediaSrc ?? defaultSrc

  const playAt = (targetSrc: string, startMs: number) => {
    if (targetSrc === src) {
      // 源未变：直接定位播放
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

  const handleCite = async (evidenceId: string) => {
    if (!evidenceId) return
    setEvidenceLoading(true)
    try {
      const ev = await getEvidence(evidenceId)
      setEvidence(ev)
      // 三类引用：
      // - 有时间引用：按证据固定资产（source_audio_asset_id）选择授权媒体回听，
      //   不无条件播放课时当前音频；源切换后等元数据就绪再定位播放；
      // - 缺时间引用：只展示原文定位，不切换媒体、不伪造播放位置；
      // - 旧版引用：仍解析到其固定来源版本并标注非当前，回听旧资产而非当前音频。
      if (ev.start_ms != null) {
        const targetSrc = ev.source_audio_asset_id
          ? `/api/audio-assets/${ev.source_audio_asset_id}/content`
          : (defaultSrc ?? undefined)
        if (targetSrc) playAt(targetSrc, ev.start_ms)
      }
    } finally {
      setEvidenceLoading(false)
    }
  }

  if (isLoading) {
    return <Skeleton className="h-96" />
  }

  if (!note) {
    return (
      <div className="flex flex-col items-center justify-center py-20">
        <FileText className="h-8 w-8 text-muted-foreground" />
        <p className="mt-3 text-sm">笔记不存在</p>
        <Button variant="outline" onClick={() => navigate('/')} className="mt-4">
          返回列表
        </Button>
      </div>
    )
  }

  const content = note.content
  const conclusions: NoteConclusion[] =
    content?.sections?.flatMap((s) => s.points || []) || content?.key_conclusions || []
  const coverage = content?.coverage

  return (
    <div className="space-y-6">
      <Button variant="ghost" onClick={() => navigate(-1)} className="-ml-2">
        <ArrowLeft className="h-4 w-4" />
        返回
      </Button>

      {/* 笔记标题与状态 */}
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-3">
            <div className="space-y-1">
              <CardTitle className="text-2xl">{content?.title || '课堂笔记'}</CardTitle>
              <div className="flex items-center gap-2">
                <Badge variant="secondary">{note.status}</Badge>
                {coverage && (
                  <span className="text-xs text-muted-foreground">
                    覆盖 {coverage.succeeded_chunks}/{coverage.total_chunks} 块
                    {coverage.complete ? '（完整）' : '（缺块）'}
                  </span>
                )}
              </div>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {/* 音频回听 */}
          {note.lecture_id && (
            <div className="mb-4 flex items-center gap-2 rounded-md border p-3">
              <FileAudio className="h-4 w-4 text-muted-foreground" />
              <span className="text-sm text-muted-foreground">回听原始音频</span>
            </div>
          )}
          <audio
            ref={audioRef}
            controls
            className="w-full"
            data-testid="note-audio"
            src={src ?? undefined}
            onLoadedMetadata={handleLoadedMetadata}
          >
            您的浏览器不支持音频播放
          </audio>

          {/* 证据面板：点击引用后展示定位/原文/版本状态 */}
          {evidence && (
            <div className="mt-4 rounded-md border bg-muted/40 p-3 text-sm">
              <div className="flex items-center gap-2">
                <Quote className="h-4 w-4 text-muted-foreground" />
                <span className="font-medium">引用定位</span>
                {!evidence.is_current && <Badge variant="warning">旧版本</Badge>}
                {evidence.start_ms != null && evidence.end_ms != null && (
                  <Badge variant="info">
                    <Clock className="mr-1 h-3 w-3" />
                    {(evidence.start_ms / 1000).toFixed(1)}s–{(evidence.end_ms / 1000).toFixed(1)}s
                  </Badge>
                )}
              </div>
              {evidence.start_ms == null && (
                <p className="mt-2 text-xs text-muted-foreground">
                  该引用来源缺时间，仅定位原文，不回听
                </p>
              )}
              {evidence.quote && (
                <p className="mt-2 italic text-muted-foreground">“{evidence.quote}”</p>
              )}
              {evidence.content && evidence.content.length > 0 && (
                <p className="mt-1 text-xs text-muted-foreground">
                  原文：{evidence.content.join('\n')}
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 结论与引用 */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <CheckCircle2 className="h-5 w-5" />
            要点与引用
            <span className="text-sm font-normal text-muted-foreground">
              （{conclusions.length} 条）
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent>
          {conclusions.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-8 text-muted-foreground">
              <FileText className="h-8 w-8 opacity-50" />
              <p className="mt-3 text-sm">暂无结论</p>
            </div>
          ) : (
            <ul className="space-y-3">
              {conclusions.map((c, i) => (
                <li key={i} className="flex flex-col gap-2 rounded-md border p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-start gap-2">
                      {statusBadge(c.status)}
                      <span className="text-sm font-medium">{c.text}</span>
                    </div>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {originLabel(c.origin)}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {c.evidence_ids?.length ? (
                      c.evidence_ids.map((evId) => (
                        <Button
                          key={evId}
                          variant="outline"
                          size="sm"
                          disabled={evidenceLoading}
                          onClick={() => handleCite(evId)}
                        >
                          <Play className="h-3 w-3" />
                          回听来源
                        </Button>
                      ))
                    ) : (
                      <span className="flex items-center gap-1 text-xs text-muted-foreground">
                        <HelpCircle className="h-3 w-3" />
                        无引用来源
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {/* 教师任务 */}
      {content?.tasks && content.tasks.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ListChecks className="h-5 w-5" />
              教师任务
            </CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-2">
              {content.tasks.map((t, i) => (
                <li key={i} className="rounded-md border p-3 text-sm">
                  {t.task}
                  {t.due_text && (
                    <span className="ml-2 text-xs text-muted-foreground">（{t.due_text}）</span>
                  )}
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
