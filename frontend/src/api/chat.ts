import { apiClient } from './client'
import { API_BASE_URL } from '@/lib/constants'
import type { ChatSession, ChatMessage, RetrievalResult } from '@/types'

// 创建会话
export async function createSession(data: {
  meeting_id?: string
  title?: string
}): Promise<ChatSession> {
  return apiClient.post('chat/sessions', { json: data }).json()
}

// 获取会话列表
export async function listSessions(meetingId?: string): Promise<ChatSession[]> {
  return apiClient
    .get('chat/sessions', {
      searchParams: meetingId ? { meeting_id: meetingId } : {},
    })
    .json()
}

// 获取会话消息
export async function getSessionMessages(
  sessionId: string,
): Promise<ChatMessage[]> {
  return apiClient.get(`chat/sessions/${sessionId}/messages`).json()
}

// 删除会话
export async function deleteSession(sessionId: string): Promise<void> {
  await apiClient.delete(`chat/sessions/${sessionId}`)
}

// SSE 流式对话
export async function* streamChat(
  sessionId: string,
  query: string,
  images?: string[],
): AsyncGenerator<{ type: string; content?: string; message?: string }> {
  const response = await fetch(
    `${API_BASE_URL}/chat/sessions/${sessionId}/stream`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, images }),
    },
  )

  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`)
  }

  const reader = response.body?.getReader()
  if (!reader) throw new Error('无法读取响应流')

  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    // 解析 SSE 事件
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const data = line.slice(6).trim()
        if (data) {
          try {
            yield JSON.parse(data)
          } catch {
            // 忽略解析错误
          }
        }
      }
    }
  }
}

// 课堂问答 SSE 流式（R5/A5：范围检索 → 流式答案 + 来源标识）
export interface ClassroomChatEvent {
  type: string
  content?: string
  status?: string
  message?: string
  results?: RetrievalResult[]
  trace?: Record<string, unknown>
}

export async function* streamClassroomChat(params: {
  query: string
  courseId?: string
  lectureId?: string
}): AsyncGenerator<ClassroomChatEvent> {
  const response = await fetch(`${API_BASE_URL}/chat/classroom/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query: params.query,
      course_id: params.courseId ?? null,
      lecture_id: params.lectureId ?? null,
    }),
  })

  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`)
  }

  const reader = response.body?.getReader()
  if (!reader) throw new Error('无法读取响应流')

  const decoder = new TextDecoder()
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })

    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const data = line.slice(6).trim()
        if (data) {
          try {
            yield JSON.parse(data) as ClassroomChatEvent
          } catch {
            // 忽略解析错误
          }
        }
      }
    }
  }
}
