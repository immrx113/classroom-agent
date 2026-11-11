import { apiClient } from './client'
import type {
  AgentRun,
  AgentRunListResponse,
  ToolListResponse,
  AgentRunStatus,
} from '@/types'

// 查询参数
export interface ListAgentRunsParams {
  meeting_id?: string
  status?: AgentRunStatus
  page?: number
  page_size?: number
}

// 列表查询
export async function listAgentRuns(
  params: ListAgentRunsParams = {},
): Promise<AgentRunListResponse> {
  return apiClient
    .get('agent-runs', {
      searchParams: {
        ...(params.meeting_id ? { meeting_id: params.meeting_id } : {}),
        ...(params.status ? { status: params.status } : {}),
        page: params.page ?? 1,
        page_size: params.page_size ?? 20,
      },
    })
    .json()
}

// 详情
export async function getAgentRun(runId: string): Promise<AgentRun> {
  return apiClient.get(`agent-runs/${runId}`).json()
}

// 审批
export interface ReviewRequest {
  action: 'approve' | 'reject'
  reviewer: string
  note?: string
}

export async function reviewAgentRun(
  runId: string,
  req: ReviewRequest,
): Promise<{ status: string; run: AgentRun }> {
  return apiClient
    .post(`agent-runs/${runId}/review`, { json: req })
    .json()
}

// 统计
export interface AgentRunStats {
  status_counts: Record<string, number>
  total_runs: number
  total_tokens: number
  total_cost_usd: number
  success_rate: number
}

export async function getAgentRunStats(): Promise<AgentRunStats> {
  return apiClient.get('agent-runs/stats/overview').json()
}

// Tool 列表
export async function listTools(): Promise<ToolListResponse> {
  return apiClient.get('agent-runs/tools/list').json()
}
