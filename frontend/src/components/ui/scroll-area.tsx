import * as React from 'react'
import { cn } from '@/lib/utils'

interface ScrollAreaProps extends React.HTMLAttributes<HTMLDivElement> {
  className?: string
}

const ScrollArea = React.forwardRef<HTMLDivElement, ScrollAreaProps>(
  ({ className, children, ...props }, ref) => {
    return (
      <div
        ref={ref}
        className={cn('overflow-y-auto overflow-x-hidden', className)}
        {...props}
      >
        {children}
      </div>
    )
  },
)
ScrollArea.displayName = 'ScrollArea'

export { ScrollArea }
