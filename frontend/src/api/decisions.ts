import { apiClient } from './client'
import type {
  DecisionListResponse,
  DecisionDetail,
  DecisionSearchResponse,
} from '@/types'

// 决策列表（分页 + 可选按 meeting 筛选）
export async function listDecisions(
  skip = 0,
  limit = 20,
  meetingId?: string,
): Promise<DecisionListResponse> {
  return apiClient
    .get('decisions', {
      searchParams: meetingId
        ? { skip, limit, meeting_id: meetingId }
        : { skip, limit },
    })
    .json()
}

// 决策语义搜索
export async function searchDecisions(
  query: string,
  topK = 5,
): Promise<DecisionSearchResponse> {
  return apiClient
    .get('decisions/search', {
      searchParams: { q: query, top_k: topK },
    })
    .json()
}

// 决策详情（含 options + 关联决策）
export async function getDecisionDetail(
  decisionId: string,
): Promise<DecisionDetail> {
  return apiClient.get(`decisions/${decisionId}`).json()
}
