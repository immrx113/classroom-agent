import { apiClient } from '@/api/client'
import type {
  Course,
  CourseDocument,
  DocumentUploadResult,
  Job,
  Lecture,
  Note,
  ParsedBlock,
  TranscriptSegment,
} from '@/types'

// ── 课程 / 课时 ──

export async function createCourse(data: {
  title: string
  teacher?: string
  term?: string
}): Promise<Course> {
  return apiClient.post('courses', { json: data }).json()
}

export async function createLecture(
  courseId: string,
  data: { title: string; description?: string; lecture_date?: string },
): Promise<Lecture> {
  return apiClient.post(`courses/${courseId}/lectures`, { json: data }).json()
}

export async function getLecture(lectureId: string): Promise<Lecture> {
  return apiClient.get(`lectures/${lectureId}`).json()
}

// ── 音频上传与转写 ──

export async function uploadLectureAudio(
  lectureId: string,
  file: File,
): Promise<{ asset_id: string; job_id: string }> {
  const formData = new FormData()
  formData.append('file', file)
  return apiClient.post(`lectures/${lectureId}/audio`, { body: formData }).json()
}

export async function listTranscriptSegments(lectureId: string): Promise<TranscriptSegment[]> {
  return apiClient.get(`lectures/${lectureId}/transcript`).json()
}

// ── 任务 ──

export async function getJob(jobId: string): Promise<Job> {
  return apiClient.get(`jobs/${jobId}`).json()
}

export async function getLatestJob(lectureId: string): Promise<Job | null> {
  return apiClient.get(`lectures/${lectureId}/job`).json()
}

export async function retryJob(jobId: string): Promise<Job> {
  return apiClient.post(`jobs/${jobId}/retry`).json()
}

// ── 课件 ──

export async function uploadLectureDocument(
  courseId: string,
  lectureId: string,
  file: File,
): Promise<DocumentUploadResult> {
  const formData = new FormData()
  formData.append('file', file)
  return apiClient
    .post(`courses/${courseId}/documents?lecture_id=${lectureId}`, { body: formData })
    .json()
}

export async function listCourseDocuments(courseId: string): Promise<CourseDocument[]> {
  return apiClient.get(`courses/${courseId}/documents`).json()
}

export async function getDocumentBlocks(id: string): Promise<ParsedBlock[]> {
  return apiClient.get(`documents/${id}/blocks`).json()
}

// ── 全课整理 / 草稿确认 / 发布 ──

export async function organizeLecture(
  lectureId: string,
  transcriptVersionId: string,
): Promise<Job> {
  return apiClient
    .post(`lectures/${lectureId}/organize`, { json: { transcript_version_id: transcriptVersionId } })
    .json()
}

export async function listNotes(lectureId: string): Promise<Note[]> {
  return apiClient.get(`lectures/${lectureId}/notes`).json()
}

export async function publishNote(noteId: string): Promise<Note> {
  return apiClient.post(`notes/${noteId}/publish`).json()
}
