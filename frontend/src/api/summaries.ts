import { apiClient } from './client'
import type {
  Summary,
  ActionItem,
  Risk,
  MeetingSummary,
  SummaryListItem,
} from '@/types'

// 获取所有纪要列表
export async function listSummaries(
  page = 1,
  pageSize = 20,
): Promise<SummaryListItem[]> {
  return apiClient
    .get('summaries', {
      searchParams: { page, page_size: pageSize },
    })
    .json()
}

// 生成纪要（触发 Multi-Agent）
export async function generateSummary(meetingId: string): Promise<Summary> {
  return apiClient.post(`meetings/${meetingId}/summarize`).json()
}

// 获取会议纪要综合数据（纪要 + 行动项 + 风险）
export async function getMeetingSummary(meetingId: string): Promise<MeetingSummary> {
  return apiClient.get(`meetings/${meetingId}/summary`).json()
}

// 获取纪要详情
export async function getSummaryDetail(meetingId: string): Promise<Summary> {
  return apiClient.get(`meetings/${meetingId}/summary/detail`).json()
}

// 获取行动项列表
export async function getActionItems(meetingId: string): Promise<ActionItem[]> {
  return apiClient.get(`meetings/${meetingId}/action-items`).json()
}

// 更新行动项
export async function updateActionItem(
  meetingId: string,
  itemId: string,
  data: { status?: string; priority?: string; assignee?: string },
): Promise<ActionItem> {
  return apiClient
    .patch(`meetings/${meetingId}/action-items/${itemId}`, { json: data })
    .json()
}

// 获取风险列表
export async function getRisks(meetingId: string): Promise<Risk[]> {
  return apiClient.get(`meetings/${meetingId}/risks`).json()
}
