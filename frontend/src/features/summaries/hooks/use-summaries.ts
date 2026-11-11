import {
  useQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import {
  listSummaries,
  generateSummary,
  getMeetingSummary,
  getSummaryDetail,
  getActionItems,
  updateActionItem,
  getRisks,
} from '@/api/summaries'
import { QUERY_KEYS } from '@/lib/constants'

// 纪要列表
export function useSummaries(page = 1, pageSize = 20) {
  return useQuery({
    queryKey: QUERY_KEYS.summaryList(page, pageSize),
    queryFn: () => listSummaries(page, pageSize),
  })
}

// 生成纪要
export function useGenerateSummary() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: generateSummary,
    onSuccess: (_, meetingId) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.meetingSummary(meetingId) })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.summaryDetail(meetingId) })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.actionItems(meetingId) })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.risks(meetingId) })
    },
  })
}

// 会议纪要综合数据
export function useMeetingSummary(meetingId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.meetingSummary(meetingId || ''),
    queryFn: () => getMeetingSummary(meetingId!),
    enabled: !!meetingId,
  })
}

// 纪要详情
export function useSummaryDetail(meetingId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.summaryDetail(meetingId || ''),
    queryFn: () => getSummaryDetail(meetingId!),
    enabled: !!meetingId,
  })
}

// 行动项列表
export function useActionItems(meetingId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.actionItems(meetingId || ''),
    queryFn: () => getActionItems(meetingId!),
    enabled: !!meetingId,
  })
}

// 更新行动项
export function useUpdateActionItem(meetingId: string | undefined) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ itemId, data }: { itemId: string; data: Parameters<typeof updateActionItem>[2] }) =>
      updateActionItem(meetingId!, itemId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.actionItems(meetingId || '') })
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.meetingSummary(meetingId || '') })
    },
  })
}

// 风险列表
export function useRisks(meetingId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.risks(meetingId || ''),
    queryFn: () => getRisks(meetingId!),
    enabled: !!meetingId,
  })
}
