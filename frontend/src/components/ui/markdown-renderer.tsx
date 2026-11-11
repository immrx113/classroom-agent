import { useState, type ComponentPropsWithoutRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeHighlight from 'rehype-highlight'
import { Check, Copy } from 'lucide-react'
import { cn } from '@/lib/utils'

interface MarkdownRendererProps {
  content: string
  className?: string
}

/**
 * Markdown 渲染器
 * - 支持 GFM（表格、任务列表、删除线、自动链接）
 * - 代码块语法高亮（rehype-highlight）
 * - 代码块带语言标签 + 一键复制
 * - 不依赖 @tailwindcss/typography，直接通过 components 自定义样式
 * - 自动去除 LLM 常见的 ```markdown 整体包裹
 */
export function MarkdownRenderer({ content, className }: MarkdownRendererProps) {
  // 去除 LLM 常见的用 ```markdown ... ``` 包裹整篇内容的情况
  const processedContent = stripCodeFence(content)

  return (
    <div
      className={cn(
        'text-sm leading-relaxed text-foreground',
        '[&>*:first-child]:mt-0 [&>*:last-child]:mb-0',
        className,
      )}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
        components={{
          h1: ({ children }) => (
            <h1 className="mt-6 mb-3 text-2xl font-bold tracking-tight text-foreground">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="mt-5 mb-2 border-b border-border pb-1 text-xl font-bold tracking-tight text-foreground">
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3 className="mt-4 mb-2 text-base font-bold text-foreground">
              {children}
            </h3>
          ),
          h4: ({ children }) => (
            <h4 className="mt-3 mb-1 text-sm font-bold text-foreground">
              {children}
            </h4>
          ),
          h5: ({ children }) => (
            <h5 className="mt-3 mb-1 text-sm font-semibold text-foreground">
              {children}
            </h5>
          ),
          h6: ({ children }) => (
            <h6 className="mt-3 mb-1 text-xs font-semibold text-muted-foreground">
              {children}
            </h6>
          ),
          p: ({ children }) => (
            <p className="my-2 leading-relaxed text-foreground">{children}</p>
          ),
          ul: ({ children }) => (
            <ul className="my-2 list-disc space-y-1 pl-5 text-foreground marker:text-muted-foreground">
              {children}
            </ul>
          ),
          ol: ({ children }) => (
            <ol className="my-2 list-decimal space-y-1 pl-5 text-foreground marker:text-muted-foreground">
              {children}
            </ol>
          ),
          li: ({ children }) => <li className="leading-relaxed">{children}</li>,
          strong: ({ children }) => (
            <strong className="font-bold text-foreground">{children}</strong>
          ),
          em: ({ children }) => (
            <em className="italic text-foreground">{children}</em>
          ),
          del: ({ children }) => (
            <del className="text-muted-foreground line-through">{children}</del>
          ),
          blockquote: ({ children }) => (
            <blockquote className="my-3 border-l-4 border-primary bg-muted/50 py-2 pl-3 italic text-muted-foreground">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-4 border-border" />,
          a: ({ children, ...props }) => (
            <a
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary underline underline-offset-2 hover:text-primary/80"
              {...props}
            >
              {children}
            </a>
          ),
          img: ({ src, alt }) => (
            <img
              src={src}
              alt={alt ?? ''}
              className="my-3 rounded-lg"
              loading="lazy"
            />
          ),
          // 行内代码
          code: ({ className, children, ...props }) => {
            // 有 language-* 类名的是代码块内的 code，由 pre 包裹，不额外加样式
            const isBlock = /language-/.test(className ?? '')
            if (isBlock) {
              return (
                <code className={className} {...props}>
                  {children}
                </code>
              )
            }
            // 行内代码
            return (
              <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[0.85em] text-foreground">
                {children}
              </code>
            )
          },
          // 代码块：包装一层容器，带语言标签和复制按钮
          pre: CodeBlock,
          // 表格：包一层可横向滚动容器
          table: ({ children }) => (
            <div className="my-3 overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                {children}
              </table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-muted">{children}</thead>,
          th: ({ children }) => (
            <th className="border border-border px-3 py-1.5 text-left font-semibold text-foreground">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-border px-3 py-1.5 text-foreground">
              {children}
            </td>
          ),
        }}
      >
        {processedContent}
      </ReactMarkdown>
    </div>
  )
}

/**
 * 代码块组件：语言标签 + 复制按钮
 * react-markdown 的 pre 节点子节点是 code，language 在 code 的 className 中
 */
type PreProps = ComponentPropsWithoutRef<'pre'>

function CodeBlock({ children, ...props }: PreProps) {
  const [copied, setCopied] = useState(false)

  // 从子 code 元素中提取语言和代码文本
  const codeChild = (Array.isArray(children) ? children[0] : children) as
    | React.ReactElement<{ className?: string; children?: React.ReactNode }>
    | undefined
  const codeProps = codeChild?.props ?? {}
  const codeClassName: string = codeProps.className ?? ''
  const match = /language-(\w+)/.exec(codeClassName)
  const language = match?.[1] ?? 'text'
  const codeText = extractText(codeProps.children)

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(codeText)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // 忽略复制失败
    }
  }

  return (
    <div className="group relative my-3 overflow-hidden rounded-lg border border-border bg-muted">
      {/* 顶部栏：语言标签 + 复制按钮 */}
      <div className="flex items-center justify-between border-b border-border bg-muted/50 px-3 py-1.5">
        <span className="text-xs font-medium text-muted-foreground">
          {language}
        </span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          aria-label="复制代码"
        >
          {copied ? (
            <>
              <Check className="h-3 w-3" />
              <span>已复制</span>
            </>
          ) : (
            <>
              <Copy className="h-3 w-3" />
              <span>复制</span>
            </>
          )}
        </button>
      </div>
      {/* 代码内容 */}
      <pre
        {...props}
        className="!my-0 !bg-transparent !p-3 overflow-x-auto text-sm leading-relaxed"
      >
        {children}
      </pre>
    </div>
  )
}

/** 从 React 节点中提取纯文本（用于复制） */
function extractText(node: React.ReactNode): string {
  if (node == null) return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (typeof node === 'object' && 'props' in node) {
    return extractText(
      (node as React.ReactElement<{ children?: React.ReactNode }>).props
        .children,
    )
  }
  return ''
}

/**
 * 去除 LLM 常见的用 ```markdown ... ``` 或 ```text ... ``` 包裹整篇内容的情况。
 * 仅当内容整体被单个代码块包裹时才处理，避免误伤正常代码块。
 */
function stripCodeFence(content: string): string {
  const trimmed = content.trim()
  // 匹配开头 ```lang 和结尾 ```
  const match = trimmed.match(/^```[a-zA-Z]*\n([\s\S]*)\n```$/)
  if (match) {
    return match[1]
  }
  return content
}
