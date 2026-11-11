import { useState } from 'react'
import {
  Dialog,
  DialogFooter,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { useReviewAgentRun } from '../hooks/use-agent-runs'
import type { AgentRun } from '@/types'

interface ReviewDialogProps {
  run: AgentRun | null
  open: boolean
  onClose: () => void
}

export function ReviewDialog({ run, open, onClose }: ReviewDialogProps) {
  const [reviewer, setReviewer] = useState('')
  const [note, setNote] = useState('')
  const [action, setAction] = useState<'approve' | 'reject'>('approve')
  const reviewMutation = useReviewAgentRun()

  const handleReset = () => {
    setReviewer('')
    setNote('')
    setAction('approve')
  }

  const handleSubmit = async () => {
    if (!run) return
    if (!reviewer.trim()) {
      alert('请输入审批人姓名')
      return
    }
    try {
      await reviewMutation.mutateAsync({
        runId: run.id,
        req: { action, reviewer: reviewer.trim(), note: note.trim() || undefined },
      })
      handleReset()
      onClose()
    } catch (e) {
      alert(`审批失败: ${e instanceof Error ? e.message : String(e)}`)
    }
  }

  const handleClose = () => {
    handleReset()
    onClose()
  }

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      title="人工审核课堂处理记录"
      description={
        run
          ? `记录: ${run.id.slice(0, 8)} · 课时: ${run.meeting_id.slice(0, 8)}`
          : undefined
      }
    >
      <div className="space-y-4">
        {/* Plan 信息 */}
        {run?.plan && (
          <div className="rounded-md border bg-muted/30 p-3 text-sm">
            <div className="mb-2 font-medium">课堂处理方案</div>
            <div className="space-y-1 text-xs text-muted-foreground">
              <div>课堂类型: {run.plan.meeting_type}</div>
              <div>
                执行节点: {[
                  run.plan.should_run_summary && 'summary',
                  run.plan.should_run_actions && 'action_items',
                  run.plan.should_run_risks && 'risks',
                ]
                  .filter(Boolean)
                  .join(' / ')}
              </div>
              <div>原因: {run.plan.reason}</div>
            </div>
          </div>
        )}

        {/* 审批人 */}
        <div className="space-y-2">
          <Label htmlFor="reviewer">审批人 *</Label>
          <Input
            id="reviewer"
            value={reviewer}
            onChange={(e) => setReviewer(e.target.value)}
            placeholder="请输入姓名"
          />
        </div>

        {/* 备注 */}
        <div className="space-y-2">
          <Label htmlFor="note">审批备注</Label>
          <Textarea
            id="note"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="可选，填写审批意见"
            rows={3}
          />
        </div>

        {/* 操作选择 */}
        <div className="flex gap-2">
          <Button
            type="button"
            variant={action === 'approve' ? 'default' : 'outline'}
            onClick={() => setAction('approve')}
            className="flex-1"
          >
            批准
          </Button>
          <Button
            type="button"
            variant={action === 'reject' ? 'destructive' : 'outline'}
            onClick={() => setAction('reject')}
            className="flex-1"
          >
            拒绝
          </Button>
        </div>
      </div>

      <DialogFooter>
        <Button variant="outline" onClick={handleClose}>
          取消
        </Button>
        <Button
          onClick={handleSubmit}
          disabled={reviewMutation.isPending}
          variant={action === 'reject' ? 'destructive' : 'default'}
        >
          {reviewMutation.isPending ? '提交中…' : '确认'}
        </Button>
      </DialogFooter>
    </Dialog>
  )
}
