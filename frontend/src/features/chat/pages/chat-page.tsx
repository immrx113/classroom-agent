import { useState, useEffect, useRef } from 'react'
import {
  Plus,
  Send,
  Trash2,
  MessageSquare,
  Loader2,
  Bot,
  Mic,
  MicOff,
  Image as ImageIcon,
  X,
  Volume2,
  Square,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'
import {
  useChatSessions,
  useSessionMessages,
  useCreateSession,
  useDeleteSession,
  useStreamChat,
} from '../hooks/use-chat'
import { ChatMessageVirtualList } from '../components/chat-message-virtual-list'
import { useSpeechRecognition } from '@/hooks/use-speech-recognition'
import { useSpeechSynthesis } from '@/hooks/use-speech-synthesis'
import type { ChatMessage } from '@/types'

const MAX_IMAGE_SIZE = 10 * 1024 * 1024 // 10MB

export default function ChatPage() {
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [input, setInput] = useState('')
  const [localMessages, setLocalMessages] = useState<ChatMessage[]>([])
  const [images, setImages] = useState<string[]>([]) // base64 data URL
  const fileInputRef = useRef<HTMLInputElement>(null)

  const { data: sessions } = useChatSessions()
  const { data: dbMessages } = useSessionMessages(currentSessionId)
  const createSession = useCreateSession()
  const deleteSession = useDeleteSession()
  const { stream, isStreaming, streamingContent, reset } = useStreamChat()

  // 语音识别
  const speechRecognition = useSpeechRecognition({
    onResult: (transcript, isFinal) => {
      if (isFinal) {
        setInput((prev) => (prev ? prev + transcript : transcript))
      }
    },
    onError: (err) => {
      console.error('语音识别错误:', err)
    },
  })

  // 语音合成
  const speechSynthesis = useSpeechSynthesis()

  // 切换会话时加载消息（仅非流式状态下同步，避免覆盖正在生成的消息）
  useEffect(() => {
    if (dbMessages && !isStreaming) {
      setLocalMessages(dbMessages)
    }
  }, [dbMessages, isStreaming])

  const handleNewSession = async () => {
    try {
      const session = await createSession.mutateAsync({})
      setCurrentSessionId(session.id)
      setLocalMessages([])
      reset()
    } catch (err) {
      console.error('创建会话失败:', err)
    }
  }

  const handleImageSelect = (files: FileList | null) => {
    if (!files || files.length === 0) return

    for (const file of Array.from(files)) {
      if (!file.type.startsWith('image/')) continue
      if (file.size > MAX_IMAGE_SIZE) {
        alert('图片大小不能超过 10MB')
        continue
      }

      const reader = new FileReader()
      reader.onload = () => {
        const dataUrl = reader.result as string
        setImages((prev) => [...prev, dataUrl])
      }
      reader.readAsDataURL(file)
    }
  }

  const removeImage = (index: number) => {
    setImages((prev) => prev.filter((_, i) => i !== index))
  }

  const handleSend = async () => {
    if ((!input.trim() && images.length === 0) || !currentSessionId || isStreaming)
      return

    const userMsg: ChatMessage = {
      id: `temp-${crypto.randomUUID()}`,
      session_id: currentSessionId,
      role: 'user',
      content: input || '(图片)',
      created_at: new Date().toISOString(),
    }
    setLocalMessages((prev) => [...prev, userMsg])
    const query = input
    const sentImages = images.length > 0 ? images : undefined
    setInput('')
    setImages([])

    await stream(
      currentSessionId,
      query,
      (fullContent) => {
        const assistantMsg: ChatMessage = {
          id: `assistant-${crypto.randomUUID()}`,
          session_id: currentSessionId,
          role: 'assistant',
          content: fullContent,
          created_at: new Date().toISOString(),
        }
        setLocalMessages((prev) => [...prev, assistantMsg])
        reset()
      },
      (err) => {
        const errorMsg: ChatMessage = {
          id: `error-${crypto.randomUUID()}`,
          session_id: currentSessionId,
          role: 'assistant',
          content: `[错误] ${err}`,
          created_at: new Date().toISOString(),
        }
        setLocalMessages((prev) => [...prev, errorMsg])
        reset()
      },
      sentImages,
    )
  }

  const handleToggleMic = () => {
    if (!speechRecognition.isSupported) {
      alert('当前浏览器不支持语音识别，请使用 Chrome / Edge / Safari')
      return
    }
    speechRecognition.toggle()
  }

  const handleToggleTTS = (text: string) => {
    if (!speechSynthesis.isSupported) {
      alert('当前浏览器不支持语音合成')
      return
    }
    speechSynthesis.toggle(text)
  }

  const handleDeleteSession = async (id: string) => {
    await deleteSession.mutateAsync(id)
    if (currentSessionId === id) {
      setCurrentSessionId(null)
      setLocalMessages([])
    }
  }

  return (
    <div className="flex h-[calc(100vh-8rem)] gap-4">
      {/* 侧边栏：会话列表 */}
      <div className="flex w-64 flex-col border-r pr-4">
        <Button onClick={handleNewSession} className="mb-3" disabled={createSession.isPending}>
          {createSession.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Plus className="h-4 w-4" />
          )}
          新建对话
        </Button>

        <ScrollArea className="flex-1">
          <div className="space-y-1">
            {sessions?.map((session) => (
              <div
                key={session.id}
                className={cn(
                  'group flex cursor-pointer items-center justify-between rounded-md px-3 py-2 text-sm transition-colors hover:bg-accent',
                  currentSessionId === session.id && 'bg-accent',
                )}
                onClick={() => setCurrentSessionId(session.id)}
              >
                <div className="flex items-center gap-2 truncate">
                  <MessageSquare className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{session.title}</span>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 opacity-0 group-hover:opacity-100"
                  onClick={(e) => {
                    e.stopPropagation()
                    handleDeleteSession(session.id)
                  }}
                >
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            ))}
          </div>
        </ScrollArea>
      </div>

      {/* 主区域：对话 */}
      <div className="flex flex-1 flex-col">
        {currentSessionId ? (
          <>
            {/* 消息列表（虚拟滚动） */}
            <div className="flex-1 overflow-hidden py-4">
              {localMessages.length === 0 && !isStreaming ? (
                <div className="flex h-full flex-col items-center justify-center text-muted-foreground">
                  <Bot className="h-12 w-12 opacity-30" />
                  <p className="mt-3 text-sm">开始与课堂学习助手对话</p>
                  <p className="mt-1 text-xs">支持文字、语音、图片多模态输入</p>
                </div>
              ) : (
                <ChatMessageVirtualList
                  messages={localMessages}
                  streamingContent={streamingContent}
                  isStreaming={isStreaming}
                  onSpeak={handleToggleTTS}
                  speakingId={speechSynthesis.isSpeaking ? 'streaming' : undefined}
                />
              )}
            </div>

            {/* 图片预览区 */}
            {images.length > 0 && (
              <div className="flex gap-2 border-t pt-2">
                {images.map((img, i) => (
                  <div key={i} className="group relative">
                    <img
                      src={img}
                      alt={`上传图片 ${i + 1}`}
                      className="h-16 w-16 rounded-md object-cover"
                    />
                    <button
                      onClick={() => removeImage(i)}
                      className="absolute -right-1 -top-1 rounded-full bg-destructive p-0.5 text-destructive-foreground opacity-0 transition-opacity group-hover:opacity-100"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* 输入框 */}
            <div className="flex items-end gap-2 border-t pt-4">
              {/* 图片上传按钮 */}
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(e) => {
                  handleImageSelect(e.target.files)
                  e.target.value = ''
                }}
              />
              <Button
                variant="outline"
                size="icon"
                onClick={() => fileInputRef.current?.click()}
                disabled={isStreaming}
                title="上传图片"
              >
                <ImageIcon className="h-4 w-4" />
              </Button>

              {/* 语音输入按钮 */}
              <Button
                variant={speechRecognition.isListening ? 'destructive' : 'outline'}
                size="icon"
                onClick={handleToggleMic}
                disabled={isStreaming}
                title={speechRecognition.isListening ? '停止语音输入' : '语音输入'}
              >
                {speechRecognition.isListening ? (
                  <MicOff className="h-4 w-4 animate-pulse" />
                ) : (
                  <Mic className="h-4 w-4" />
                )}
              </Button>

              <Input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && !e.shiftKey && handleSend()}
                placeholder={
                  speechRecognition.isListening ? '正在聆听...' : '输入问题，Enter 发送...'
                }
                disabled={isStreaming}
                className="flex-1"
              />

              {/* TTS 朗读按钮（朗读最后一条 AI 回复） */}
              {localMessages.length > 0 &&
                localMessages[localMessages.length - 1].role === 'assistant' &&
                !isStreaming && (
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() =>
                      handleToggleTTS(localMessages[localMessages.length - 1].content)
                    }
                    title={speechSynthesis.isSpeaking ? '停止朗读' : '朗读回复'}
                  >
                    {speechSynthesis.isSpeaking ? (
                      <Square className="h-4 w-4" />
                    ) : (
                      <Volume2 className="h-4 w-4" />
                    )}
                  </Button>
                )}

              <Button onClick={handleSend} disabled={(!input.trim() && images.length === 0) || isStreaming}>
                {isStreaming ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Send className="h-4 w-4" />
                )}
              </Button>
            </div>
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center text-muted-foreground">
            <Bot className="h-16 w-16 opacity-20" />
            <p className="mt-4 text-sm">选择或新建对话开始</p>
            <p className="mt-1 text-xs">支持语音输入和图片上传</p>
          </div>
        )}
      </div>
    </div>
  )
}
