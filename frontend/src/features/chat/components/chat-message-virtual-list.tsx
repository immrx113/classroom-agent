import { memo, useEffect, useRef } from 'react'
import { Loader2, Bot, User, FileText, Volume2, Square } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { MarkdownRenderer } from '@/components/ui/markdown-renderer'
import { useVirtualList } from '@/hooks/use-virtual-list'
import { cn } from '@/lib/utils'
import type { ChatMessage } from '@/types'

interface ChatMessageVirtualListProps {
  messages: ChatMessage[]
  /** 流式输出中的临时内容 */
  streamingContent?: string
  isStreaming?: boolean
  /** 朗读回调 */
  onSpeak?: (text: string) => void
  /** 正在朗读的消息 ID */
  speakingId?: string
  className?: string
}

function ChatMessageVirtualListBase({
  messages,
  streamingContent,
  isStreaming,
  onSpeak,
  speakingId,
  className,
}: ChatMessageVirtualListProps) {
  // 包含流式消息的总数
  const hasStreaming = isStreaming && streamingContent !== undefined
  const count = messages.length + (hasStreaming ? 1 : 0)

  const { parentRef, virtualizer, items, totalSize, scrollToBottom } =
    useVirtualList({
      count,
      estimateSize: 120,
      overscan: 4,
    })

  // 新消息时自动滚动到底部
  const prevCount = useRef(0)
  useEffect(() => {
    if (count > prevCount.current) {
      scrollToBottom()
    }
    prevCount.current = count
  }, [count, scrollToBottom])

  // 流式内容变化时滚动
  useEffect(() => {
    if (hasStreaming) {
      scrollToBottom()
    }
  }, [streamingContent, hasStreaming, scrollToBottom])

  return (
    <div
      ref={parentRef}
      className={cn('h-full overflow-y-auto pr-2', className)}
      style={{ contain: 'strict' }}
    >
      <div
        style={{
          height: `${totalSize}px`,
          width: '100%',
          position: 'relative',
        }}
      >
        {items.map((virtualItem) => {
          const isStreamingItem =
            hasStreaming && virtualItem.index === messages.length
          const message = isStreamingItem
            ? null
            : messages[virtualItem.index]
          if (!message && !isStreamingItem) return null

          const isUser = message?.role === 'user'
          const content = isStreamingItem ? streamingContent : message?.content || ''

          return (
            <div
              key={isStreamingItem ? 'streaming' : message!.id}
              data-index={virtualItem.index}
              ref={virtualizer.measureElement}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: '100%',
                transform: `translateY(${virtualItem.start}px)`,
              }}
              className="py-2"
            >
              <div className={cn('flex gap-3', isUser && 'flex-row-reverse')}>
                <div
                  className={cn(
                    'flex h-8 w-8 shrink-0 items-center justify-center rounded-full',
                    isUser ? 'bg-primary' : 'bg-primary/10',
                  )}
                >
                  {isUser ? (
                    <User className="h-4 w-4 text-primary-foreground" />
                  ) : (
                    <Bot className="h-4 w-4 text-primary" />
                  )}
                </div>
                <div className={cn('flex-1', isUser && 'flex flex-col items-end')}>
                  <div
                    className={cn(
                      'inline-block max-w-[85%] rounded-lg p-3',
                      isUser
                        ? 'bg-primary text-primary-foreground'
                        : 'bg-muted',
                    )}
                  >
                    {isUser ? (
                      <p className="whitespace-pre-wrap text-sm">{content}</p>
                    ) : content ? (
                      <MarkdownRenderer content={content} />
                    ) : (
                      <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                    )}
                  </div>

                  {/* 知识来源 */}
                  {message?.metadata?.sources &&
                    message.metadata.sources.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1">
                        <span className="text-xs text-muted-foreground">
                          知识来源：
                        </span>
                        {message.metadata.sources.map((src, i) => (
                          <Badge key={i} variant="secondary" className="text-xs">
                            <FileText className="mr-1 h-3 w-3" />
                            {src.title}
                          </Badge>
                        ))}
                      </div>
                    )}

                  {/* 朗读按钮（仅 assistant 消息） */}
                  {!isUser && !isStreamingItem && onSpeak && content && (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="mt-1 h-6 px-2 text-xs text-muted-foreground"
                      onClick={() => onSpeak(content)}
                    >
                      {speakingId === message!.id ? (
                        <>
                          <Square className="mr-1 h-3 w-3" />
                          停止
                        </>
                      ) : (
                        <>
                          <Volume2 className="mr-1 h-3 w-3" />
                          朗读
                        </>
                      )}
                    </Button>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export const ChatMessageVirtualList = memo(ChatMessageVirtualListBase)
