import { apiClient } from './client'
import { API_BASE_URL } from '@/lib/constants'
import type { KnowledgeDocument, KnowledgeSearchResponse } from '@/types'

// 索引文本
export async function indexText(data: {
  title: string
  content: string
  source_type?: string
  metadata?: Record<string, unknown>
}): Promise<KnowledgeDocument[]> {
  return apiClient.post('knowledge/index', { json: data }).json()
}

// 上传文档并索引
export async function uploadDocument(
  file: File,
  onProgress?: (percent: number) => void,
): Promise<KnowledgeDocument[]> {
  const formData = new FormData()
  formData.append('file', file)

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()

    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100))
      }
    })

    xhr.addEventListener('load', () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText))
      } else {
        let msg = `上传失败: ${xhr.status}`
        try {
          const errBody = JSON.parse(xhr.responseText)
          if (errBody.detail) msg = errBody.detail
        } catch {
          // 非 JSON 响应
        }
        reject(new Error(msg))
      }
    })

    xhr.addEventListener('error', () => reject(new Error('网络错误')))
    xhr.addEventListener('abort', () => reject(new Error('上传已取消')))

    xhr.open('POST', `${API_BASE_URL}/knowledge/upload`)
    xhr.send(formData)
  })
}

// 知识检索
export async function searchKnowledge(
  query: string,
  topK = 5,
): Promise<KnowledgeSearchResponse> {
  return apiClient
    .post('knowledge/search', { json: { query, top_k: topK } })
    .json()
}

// 获取知识文档列表
export async function listKnowledgeDocuments(
  page = 1,
  pageSize = 20,
): Promise<KnowledgeDocument[]> {
  return apiClient
    .get('knowledge/documents', {
      searchParams: { page, page_size: pageSize },
    })
    .json()
}

// 删除知识文档
export async function deleteKnowledgeDocument(id: string): Promise<void> {
  await apiClient.delete(`knowledge/documents/${id}`).json()
}
