import { useState, useCallback } from 'react'
import {
  useQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import {
  createSession,
  listSessions,
  getSessionMessages,
  deleteSession,
  streamChat,
} from '@/api/chat'
import { QUERY_KEYS } from '@/lib/constants'

// 会话列表
export function useChatSessions(meetingId?: string) {
  return useQuery({
    queryKey: QUERY_KEYS.chatSessions(meetingId || ''),
    queryFn: () => listSessions(meetingId),
  })
}

// 会话消息
export function useSessionMessages(sessionId: string | null) {
  return useQuery({
    queryKey: QUERY_KEYS.chatMessages(sessionId || ''),
    queryFn: () => getSessionMessages(sessionId!),
    enabled: !!sessionId,
  })
}

// 创建会话
export function useCreateSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: createSession,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['chat-sessions'] })
    },
  })
}

// 删除会话
export function useDeleteSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deleteSession,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['chat-sessions'] })
    },
  })
}

// 流式对话
export function useStreamChat() {
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')

  const stream = useCallback(
    async (
      sessionId: string,
      query: string,
      onDone?: (fullContent: string) => void,
      onError?: (err: string) => void,
      images?: string[],
    ) => {
      setIsStreaming(true)
      setStreamingContent('')

      try {
        let full = ''
        for await (const event of streamChat(sessionId, query, images)) {
          if (event.type === 'token' && event.content) {
            full += event.content
            setStreamingContent(full)
          } else if (event.type === 'done') {
            onDone?.(full)
          } else if (event.type === 'error') {
            onError?.(event.message || '未知错误')
          }
        }
      } catch (err) {
        onError?.(err instanceof Error ? err.message : '请求失败')
      } finally {
        setIsStreaming(false)
      }
    },
    [],
  )

  const reset = useCallback(() => {
    setStreamingContent('')
    setIsStreaming(false)
  }, [])

  return { stream, isStreaming, streamingContent, reset }
}
