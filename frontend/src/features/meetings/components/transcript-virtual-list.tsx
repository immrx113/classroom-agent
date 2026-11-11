import { memo } from 'react'
import { useVirtualList } from '@/hooks/use-virtual-list'
import { formatDuration } from '@/lib/utils'
import type { Transcript } from '@/types'

interface TranscriptVirtualListProps {
  transcripts: Transcript[]
  className?: string
}

function TranscriptVirtualListBase({
  transcripts,
  className,
}: TranscriptVirtualListProps) {
  const { parentRef, virtualizer, items, totalSize } = useVirtualList({
    count: transcripts.length,
    estimateSize: 80, // 预估每条高度
    overscan: 6,
  })

  if (transcripts.length === 0) return null

  return (
    <div
      ref={parentRef}
      className={`max-h-[600px] overflow-y-auto pr-1 ${className ?? ''}`}
    >
      <div
        style={{
          height: `${totalSize}px`,
          width: '100%',
          position: 'relative',
        }}
      >
        {items.map((virtualItem) => {
          const transcript = transcripts[virtualItem.index]
          if (!transcript) return null

          return (
            <div
              key={transcript.id}
              data-index={virtualItem.index}
              ref={virtualizer.measureElement}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: '100%',
                transform: `translateY(${virtualItem.start}px)`,
              }}
              className="py-1.5"
            >
              <div className="flex gap-3 rounded-md border p-3">
                {/* 说话人 */}
                <div className="flex shrink-0 flex-col items-center">
                  <div className="flex h-8 w-8 items-center justify-center rounded-full bg-primary/10 text-xs font-medium text-primary">
                    {transcript.speaker?.charAt(0) || '?'}
                  </div>
                  <span className="mt-1 text-xs text-muted-foreground">
                    {formatDuration(transcript.start_time || 0)}
                  </span>
                </div>

                {/* 内容 */}
                <div className="flex-1">
                  <p className="text-xs font-medium text-muted-foreground">
                    {transcript.speaker || '未知说话人'}
                  </p>
                  <p className="mt-0.5 text-sm">{transcript.content}</p>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export const TranscriptVirtualList = memo(TranscriptVirtualListBase)
