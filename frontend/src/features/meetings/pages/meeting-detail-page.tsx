import { useParams, useNavigate } from 'react-router-dom'
import {
  ArrowLeft,
  Clock,
  Users,
  FileAudio,
  FileText,
  Loader2,
  AlertCircle,
  MessageSquare,
  Sparkles,
  Trash2,
  GitBranch,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { MeetingStatusBadge } from '../components/meeting-status-badge'
import { TranscriptVirtualList } from '../components/transcript-virtual-list'
import { useMeeting, useTranscripts, useTranscriptionStatus, useDeleteMeeting } from '../hooks/use-meetings'
import { useDecisions } from '../../decisions/hooks/use-decisions'
import { useCurrentNote } from '../../notes/hooks/use-notes'
import { formatDateTime } from '@/lib/utils'

export default function MeetingDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const { data: meeting, isLoading: meetingLoading } = useMeeting(id)
  const { data: transcripts, isLoading: transcriptsLoading } = useTranscripts(id)
  const { data: transcriptionStatus } = useTranscriptionStatus(id)
  const deleteMeeting = useDeleteMeeting()
  const { data: decisions, isLoading: decisionsLoading } = useDecisions(0, 100, id)
  const { data: currentNote } = useCurrentNote(id)

  const isProcessing =
    transcriptionStatus?.status === 'transcribing' ||
    transcriptionStatus?.status === 'pending'

  const handleDelete = async () => {
    if (!meeting) return
    if (!confirm(`确定删除课时「${meeting.title}」吗？\n关联的转写记录和课堂摘要也将被删除。`)) return
    await deleteMeeting.mutateAsync(meeting.id)
    navigate('/')
  }

  return (
    <div className="space-y-6">
      {/* 返回按钮 */}
      <Button variant="ghost" onClick={() => navigate('/')} className="-ml-2">
        <ArrowLeft className="h-4 w-4" />
        返回列表
      </Button>

      {meetingLoading ? (
        <Skeleton className="h-32" />
      ) : meeting ? (
        <>
          {/* 会议信息 */}
          <Card>
            <CardHeader>
              <div className="flex items-start justify-between">
                <div className="space-y-1">
                  <CardTitle className="text-2xl">{meeting.title}</CardTitle>
                  <div className="flex items-center gap-3">
                    <MeetingStatusBadge status={meeting.status} />
                    <span className="text-xs text-muted-foreground">
                      创建于 {formatDateTime(meeting.created_at)}
                    </span>
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  {currentNote && (
                    <Button variant="outline" onClick={() => navigate(`/notes/${currentNote.id}`)}>
                      <FileText className="h-4 w-4" />
                      课堂笔记
                    </Button>
                  )}
                  {meeting.status === 'processed' && (
                    <Button onClick={() => navigate(`/summaries/${meeting.id}`)}>
                      <Sparkles className="h-4 w-4" />
                      生成课堂摘要
                    </Button>
                  )}
                  <Button
                    variant="outline"
                    onClick={handleDelete}
                    disabled={deleteMeeting.isPending}
                  >
                    <Trash2 className="h-4 w-4" />
                    删除
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <div className="grid gap-4 sm:grid-cols-3">
                {meeting.start_time && (
                  <InfoItem
                    icon={<Clock className="h-4 w-4" />}
                    label="开始时间"
                    value={formatDateTime(meeting.start_time)}
                  />
                )}
                {meeting.participants && meeting.participants.length > 0 && (
                  <InfoItem
                    icon={<Users className="h-4 w-4" />}
                    label="课堂参与者"
                    value={meeting.participants.join('、')}
                  />
                )}
                {meeting.audio_url && (
                  <InfoItem
                    icon={<FileAudio className="h-4 w-4" />}
                    label="录音文件"
                    value="已上传"
                  />
                )}
              </div>

              {meeting.description && (
                <div className="mt-4 rounded-md bg-muted p-3">
                  <p className="text-sm">{meeting.description}</p>
                </div>
              )}

              {/* 音频播放器 */}
              {meeting.audio_url && (
                <div className="mt-4">
                  <audio controls className="w-full">
                    <source
                      src={`/api/meetings/${meeting.id}/audio`}
                      type="audio/mpeg"
                    />
                    您的浏览器不支持音频播放
                  </audio>
                </div>
              )}
            </CardContent>
          </Card>

          {/* 转写记录 */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <MessageSquare className="h-5 w-5" />
                转写记录
                {transcriptionStatus && (
                  <span className="text-sm font-normal text-muted-foreground">
                    ({transcriptionStatus.transcript_count} 条)
                  </span>
                )}
              </CardTitle>
              {/* 本地 Mock 转写提示 */}
              {meeting.transcription_mode === 'mock' && (
                <div className="mt-3 flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900 dark:border-amber-700 dark:bg-amber-950/50 dark:text-amber-200">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <div className="text-xs">
                    <p className="font-semibold">本地开发模式 · 模拟转写</p>
                    <p className="mt-0.5 text-amber-800 dark:text-amber-300">
                      当前转写内容为模拟数据（DashScope OpenAI 兼容接口不支持音频转写）。
                      生产环境可配置 DashScope 原生录音识别 API + 公网文件 URL 启用真实转写。
                    </p>
                  </div>
                </div>
              )}
              {meeting.transcription_mode === 'real' && (
                <div className="mt-3 flex items-start gap-2 rounded-md border border-emerald-300 bg-emerald-50 p-3 text-emerald-900 dark:border-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-200">
                  <Sparkles className="mt-0.5 h-4 w-4 shrink-0" />
                  <div className="text-xs">
                    <p className="font-semibold">真实转写 · API 模式</p>
                    <p className="mt-0.5 text-emerald-800 dark:text-emerald-300">
                      转写内容由 DashScope Paraformer 语音识别 API 生成。
                    </p>
                  </div>
                </div>
              )}
            </CardHeader>
            <CardContent>
              {/* 转写中状态 */}
              {isProcessing && (
                <div className="flex flex-col items-center justify-center py-12">
                  <Loader2 className="h-8 w-8 animate-spin text-primary" />
                  <p className="mt-3 text-sm font-medium">
                    {transcriptionStatus?.status === 'pending'
                      ? '等待转写...'
                      : '正在转写音频...'}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    转写完成后将自动显示结果
                  </p>
                </div>
              )}

              {/* 转写失败 */}
              {meeting.status === 'failed' && (
                <div className="flex flex-col items-center justify-center py-12">
                  <AlertCircle className="h-8 w-8 text-destructive" />
                  <p className="mt-3 text-sm font-medium">转写失败</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    请检查音频文件是否有效
                  </p>
                </div>
              )}

              {/* 转写记录列表 */}
              {!isProcessing && meeting.status !== 'failed' && (
                <>
                  {transcriptsLoading ? (
                    <div className="space-y-3">
                      {[...Array(5)].map((_, i) => (
                        <Skeleton key={i} className="h-16" />
                      ))}
                    </div>
                  ) : transcripts && transcripts.length > 0 ? (
                    <TranscriptVirtualList transcripts={transcripts} />
                  ) : (
                    <div className="flex flex-col items-center justify-center py-12">
                      <MessageSquare className="h-8 w-8 text-muted-foreground/50" />
                      <p className="mt-3 text-sm font-medium">暂无转写记录</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        上传音频后将自动生成转写
                      </p>
                    </div>
                  )}
                </>
              )}
            </CardContent>
          </Card>

          {/* 决策记录 */}
          {meeting.status === 'processed' && (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <GitBranch className="h-5 w-5" />
                    课堂讨论记录
                    {decisions && (
                      <span className="text-sm font-normal text-muted-foreground">
                        ({decisions.total || decisions.items.length} 条)
                      </span>
                    )}
                  </div>
                  {decisions && decisions.total > 0 && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => navigate(`/decisions?meetingId=${meeting.id}`)}
                    >
                      查看全部
                    </Button>
                  )}
                </CardTitle>
              </CardHeader>
              <CardContent>
                {decisionsLoading ? (
                  <div className="space-y-3">
                    {[...Array(3)].map((_, i) => (
                      <Skeleton key={i} className="h-24" />
                    ))}
                  </div>
                ) : decisions && decisions.items.length > 0 ? (
                  <div className="space-y-3">
                    {decisions.items.slice(0, 5).map((decision) => (
                      <div
                        key={decision.id}
                        className="cursor-pointer rounded-md border p-3 transition-colors hover:bg-muted"
                        onClick={() => navigate(`/decisions/${decision.id}`)}
                      >
                        <div className="mb-2 flex items-start justify-between gap-2">
                          <h3 className="line-clamp-2 text-sm font-semibold">
                            {decision.title}
                          </h3>
                          {decision.confidence != null && (
                            <Badge variant="secondary" className="shrink-0">
                              {(decision.confidence * 100).toFixed(0)}%
                            </Badge>
                          )}
                        </div>
                        {decision.chosen_option && (
                          <p className="line-clamp-1 text-sm text-muted-foreground">
                            <span className="font-medium text-foreground">讨论结论：</span>
                            {decision.chosen_option}
                          </p>
                        )}
                      </div>
                    ))}
                    {decisions.total > 5 && (
                      <div className="text-center">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => navigate(`/decisions?meetingId=${meeting.id}`)}
                        >
                          还有 {decisions.total - 5} 条讨论记录 →
                        </Button>
                      </div>
                    )}
                  </div>
                ) : (
                  <div className="flex flex-col items-center justify-center py-12">
                    <GitBranch className="h-8 w-8 text-muted-foreground/50" />
                    <p className="mt-3 text-sm font-medium">暂无课堂讨论记录</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      这里展示已提取的课堂讨论内容
                    </p>
                  </div>
                )}
              </CardContent>
            </Card>
          )}
        </>
      ) : (
        <div className="flex flex-col items-center justify-center py-20">
          <AlertCircle className="h-8 w-8 text-muted-foreground" />
          <p className="mt-3 text-sm">课时不存在</p>
          <Button variant="outline" onClick={() => navigate('/')} className="mt-4">
            返回列表
          </Button>
        </div>
      )}
    </div>
  )
}

function InfoItem({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode
  label: string
  value: string
}) {
  return (
    <div className="flex items-center gap-2">
      <div className="flex h-8 w-8 items-center justify-center rounded-md bg-muted text-muted-foreground">
        {icon}
      </div>
      <div>
        <p className="text-xs text-muted-foreground">{label}</p>
        <p className="text-sm font-medium">{value}</p>
      </div>
    </div>
  )
}
