import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { Button } from './button'
import { cn } from '@/lib/utils'

interface DialogProps {
  open: boolean
  onClose: () => void
  title?: string
  description?: string
  children: ReactNode
  className?: string
  /** 是否允许点击遮罩关闭，默认 true */
  closeOnOverlayClick?: boolean
  /** 是否显示关闭按钮，默认 true */
  showCloseButton?: boolean
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  className,
  closeOnOverlayClick = true,
  showCloseButton = true,
}: DialogProps) {
  const contentRef = useRef<HTMLDivElement>(null)

  // ESC 关闭
  useEffect(() => {
    if (!open) return
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open, onClose])

  // 打开时锁定 body 滚动
  useEffect(() => {
    if (!open) return
    const original = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = original
    }
  }, [open])

  // 打开时自动聚焦内容
  useEffect(() => {
    if (open) {
      // 延迟聚焦，等待动画完成
      const timer = setTimeout(() => {
        const firstInput = contentRef.current?.querySelector<HTMLElement>(
          'input, textarea, button, [tabindex]:not([tabindex="-1"])',
        )
        firstInput?.focus()
      }, 100)
      return () => clearTimeout(timer)
    }
  }, [open])

  if (!open) return null

  return (
    <div
      className="dialog-overlay fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      onClick={(e) => {
        if (closeOnOverlayClick && e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={contentRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cn(
          'dialog-content relative flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-xl border border-border bg-background shadow-2xl',
          className,
        )}
      >
        {/* 头部 */}
        {(title || showCloseButton) && (
          <div className="flex items-start justify-between gap-4 border-b border-border px-6 py-4">
            <div className="min-w-0 flex-1">
              {title && (
                <h2 className="text-lg font-semibold leading-tight">{title}</h2>
              )}
              {description && (
                <p className="mt-1 text-sm text-muted-foreground">{description}</p>
              )}
            </div>
            {showCloseButton && (
              <Button
                variant="ghost"
                size="icon"
                className="-mr-2 -mt-1 shrink-0"
                onClick={onClose}
              >
                <X className="h-4 w-4" />
              </Button>
            )}
          </div>
        )}

        {/* 内容区（可滚动） */}
        <div className="flex-1 overflow-y-auto px-6 py-4">{children}</div>
      </div>
    </div>
  )
}

// 底部操作栏
export function DialogFooter({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center justify-end gap-2 border-t border-border px-6 py-4">
      {children}
    </div>
  )
}
