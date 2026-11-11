import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Dialog, DialogFooter } from '@/components/ui/dialog'
import { AudioUploader } from './audio-uploader'
import { useCreateMeeting } from '../hooks/use-meetings'
import { uploadAudio } from '@/api/meetings'

interface CreateMeetingDialogProps {
  open: boolean
  onClose: () => void
}

export function CreateMeetingDialog({ open, onClose }: CreateMeetingDialogProps) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [participants, setParticipants] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [uploadProgress, setUploadProgress] = useState(0)
  const [isUploading, setIsUploading] = useState(false)
  const [error, setError] = useState('')

  const createMeeting = useCreateMeeting()

  const resetForm = () => {
    setTitle('')
    setDescription('')
    setParticipants('')
    setFile(null)
    setUploadProgress(0)
    setIsUploading(false)
    setError('')
  }

  const handleClose = () => {
    if (isUploading) return
    resetForm()
    onClose()
  }

  const handleSubmit = async () => {
    setError('')

    if (!title.trim()) {
      setError('请输入课时标题')
      return
    }

    try {
      const meeting = await createMeeting.mutateAsync({
        title: title.trim(),
        description: description.trim() || undefined,
        participants: participants
          ? participants.split(',').map((p) => p.trim()).filter(Boolean)
          : undefined,
      })

      if (file) {
        setIsUploading(true)
        await uploadAudio(meeting.id, file, (percent) => {
          setUploadProgress(percent)
        })
      }

      resetForm()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setIsUploading(false)
    }
  }

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      title="新建课时"
      description="填写课时信息，整理课堂录音与学习资料"
      closeOnOverlayClick={!isUploading}
    >
      <div className="space-y-5">
        {/* 标题 */}
        <div className="space-y-2">
          <Label htmlFor="title">
            课时标题 <span className="text-destructive">*</span>
          </Label>
          <Input
            id="title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="例如：大学物理 · 牛顿第二定律"
            autoFocus
          />
        </div>

        {/* 描述 */}
        <div className="space-y-2">
          <Label htmlFor="description">课时描述</Label>
          <Textarea
            id="description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="简要描述本节课的学习内容..."
            rows={3}
          />
        </div>

        {/* 参会人 */}
        <div className="space-y-2">
          <Label htmlFor="participants">课堂参与者</Label>
          <Input
            id="participants"
            value={participants}
            onChange={(e) => setParticipants(e.target.value)}
            placeholder="用逗号分隔，如：张三, 李四, 王五"
          />
        </div>

        {/* 音频上传 */}
        <div className="space-y-2">
          <Label>录音文件</Label>
          <AudioUploader
            onFileSelect={setFile}
            selectedFile={file}
            uploadProgress={uploadProgress}
            isUploading={isUploading}
          />
        </div>

        {/* 错误提示 */}
        {error && (
          <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}
      </div>

      <DialogFooter>
        <Button variant="outline" onClick={handleClose} disabled={isUploading}>
          取消
        </Button>
        <Button
          onClick={handleSubmit}
          disabled={isUploading || createMeeting.isPending}
        >
          {isUploading
            ? `上传中 ${uploadProgress}%`
            : createMeeting.isPending
              ? '创建中...'
              : '创建课时'}
        </Button>
      </DialogFooter>
    </Dialog>
  )
}
