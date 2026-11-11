import { apiClient } from './client'
import { API_BASE_URL } from '@/lib/constants'
import type {
  Meeting,
  Transcript,
  MeetingStatus,
} from '@/types'

// 创建会议
export async function createMeeting(data: {
  title: string
  description?: string
  participants?: string[]
  start_time?: string
  end_time?: string
}): Promise<Meeting> {
  return apiClient.post('meetings', { json: data }).json()
}

// 获取会议列表
export async function listMeetings(
  page = 1,
  pageSize = 20,
): Promise<Meeting[]> {
  return apiClient
    .get('meetings', {
      searchParams: { page, page_size: pageSize },
    })
    .json()
}

// 获取会议详情
export async function getMeeting(id: string): Promise<Meeting> {
  return apiClient.get(`meetings/${id}`).json()
}

// 更新会议
export async function updateMeeting(
  id: string,
  data: Partial<Pick<Meeting, 'title' | 'description' | 'participants'>>,
): Promise<Meeting> {
  return apiClient.patch(`meetings/${id}`, { json: data }).json()
}

// 删除会议
export async function deleteMeeting(id: string): Promise<void> {
  await apiClient.delete(`meetings/${id}`)
}

// 上传音频文件
export async function uploadAudio(
  meetingId: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<Meeting> {
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
        reject(new Error(`上传失败: ${xhr.statusText}`))
      }
    })

    xhr.addEventListener('error', () => reject(new Error('网络错误')))
    xhr.addEventListener('abort', () => reject(new Error('上传已取消')))

    xhr.open('POST', `${API_BASE_URL}/meetings/${meetingId}/upload`)
    xhr.send(formData)
  })
}

// 获取转写记录
export async function getTranscripts(meetingId: string): Promise<Transcript[]> {
  return apiClient.get(`meetings/${meetingId}/transcripts`).json()
}

// 获取转写状态
export async function getTranscriptionStatus(
  meetingId: string,
): Promise<{ meeting_id: string; status: MeetingStatus; transcript_count: number }> {
  return apiClient.get(`meetings/${meetingId}/transcription-status`).json()
}
