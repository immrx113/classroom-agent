interface HighlightTextProps {
  text: string
  keywords: string
  className?: string
}

export function HighlightText({ text, keywords, className }: HighlightTextProps) {
  if (!keywords.trim()) {
    return <span className={className}>{text}</span>
  }

  // 转义正则特殊字符并按空格分割
  const terms = keywords
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))

  if (terms.length === 0) {
    return <span className={className}>{text}</span>
  }

  const regex = new RegExp(`(${terms.join('|')})`, 'gi')
  const parts = text.split(regex)

  return (
    <span className={className}>
      {parts.map((part, i) => {
        if (regex.test(part)) {
          // 重置 regex lastIndex
          regex.lastIndex = 0
          return (
            <mark key={i} className="rounded bg-yellow-200 px-0.5 dark:bg-yellow-900">
              {part}
            </mark>
          )
        }
        return part
      })}
    </span>
  )
}
