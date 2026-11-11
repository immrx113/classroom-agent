import { apiClient } from '@/api/client'
import type {
  CourseDocument,
  ParsedBlock,
  RetrievalSearchResponse,
} from '@/types'

// 范围检索（显式课程/课时范围）
export async function searchRetrieval(params: {
  query: string
  courseId?: string
  lectureId?: string
  topK?: number
}): Promise<RetrievalSearchResponse> {
  return apiClient
    .post('retrieval/search', {
      json: {
        query: params.query,
        course_id: params.courseId ?? null,
        lecture_id: params.lectureId ?? null,
        top_k: params.topK,
      },
    })
    .json()
}

// 课程文档列表
export async function listCourseDocuments(courseId: string): Promise<CourseDocument[]> {
  return apiClient.get(`courses/${courseId}/documents`).json()
}

// 上传课件到课程（可选指定课时）
export async function uploadCourseDocument(
  courseId: string,
  file: File,
  lectureId?: string,
): Promise<CourseDocument> {
  const formData = new FormData()
  formData.append('file', file)
  const search = lectureId ? `?lecture_id=${lectureId}` : ''
  return apiClient
    .post(`courses/${courseId}/documents${search}`, { body: formData })
    .json()
}

// 删除文档（按身份）
export async function deleteDocument(id: string): Promise<void> {
  await apiClient.delete(`documents/${id}`).json()
}

// 读取文档解析块（真实定位）
export async function getDocumentBlocks(id: string): Promise<ParsedBlock[]> {
  return apiClient.get(`documents/${id}/blocks`).json()
}
