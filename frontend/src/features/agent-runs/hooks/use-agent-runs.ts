import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  listAgentRuns,
  getAgentRun,
  reviewAgentRun,
  getAgentRunStats,
  listTools,
  type ListAgentRunsParams,
  type ReviewRequest,
} from '@/api/agent-runs'
import { QUERY_KEYS } from '@/lib/constants'

// 列表
export function useAgentRuns(params: ListAgentRunsParams = {}) {
  return useQuery({
    queryKey: QUERY_KEYS.agentRunsList(params as Record<string, unknown>),
    queryFn: () => listAgentRuns(params),
    // running / paused 状态自动 5s 轮询；列表为空不轮询
    refetchInterval: (query) => {
      const items = query.state.data?.items ?? []
      if (items.length === 0) return false
      const hasActive = items.some(
        (r) => r.status === 'running' || r.status === 'paused' || r.status === 'pending',
      )
      return hasActive ? 5000 : false
    },
  })
}

// 详情
export function useAgentRun(runId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.agentRun(runId || ''),
    queryFn: () => getAgentRun(runId!),
    enabled: !!runId,
    // 终态（succeeded/failed/cancelled）停止轮询
    refetchInterval: (query) => {
      const r = query.state.data
      if (!r) return false
      if (r.status === 'running' || r.status === 'paused' || r.status === 'pending') {
        return 3000
      }
      return false
    },
  })
}

// 审批
export function useReviewAgentRun() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ runId, req }: { runId: string; req: ReviewRequest }) =>
      reviewAgentRun(runId, req),
    onSuccess: (_, { runId }) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.agentRun(runId) })
      queryClient.invalidateQueries({ queryKey: [QUERY_KEYS.agentRuns] })
      queryClient.invalidateQueries({ queryKey: [QUERY_KEYS.agentRunStats] })
    },
  })
}

// 统计
export function useAgentRunStats() {
  return useQuery({
    queryKey: [QUERY_KEYS.agentRunStats],
    queryFn: getAgentRunStats,
    refetchInterval: 10000,
  })
}

// 工具列表
export function useTools() {
  return useQuery({
    queryKey: [QUERY_KEYS.tools],
    queryFn: listTools,
    staleTime: 60_000,
  })
}
