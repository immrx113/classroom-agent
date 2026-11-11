import {
  useQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import {
  listMeetings,
  getMeeting,
  createMeeting,
  updateMeeting,
  deleteMeeting,
  getTranscripts,
  getTranscriptionStatus,
} from '@/api/meetings'
import { QUERY_KEYS } from '@/lib/constants'

// 会议列表
export function useMeetings(page = 1, pageSize = 20) {
  return useQuery({
    queryKey: [QUERY_KEYS.meetings, page, pageSize],
    queryFn: () => listMeetings(page, pageSize),
  })
}

// 会议详情
export function useMeeting(id: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.meeting(id || ''),
    queryFn: () => getMeeting(id!),
    enabled: !!id,
  })
}

// 创建会议
export function useCreateMeeting() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: createMeeting,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [QUERY_KEYS.meetings] })
    },
  })
}

// 更新会议
export function useUpdateMeeting() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, data }: { id: string; data: Parameters<typeof updateMeeting>[1] }) =>
      updateMeeting(id, data),
    onSuccess: (_, { id }) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.meeting(id) })
      queryClient.invalidateQueries({ queryKey: [QUERY_KEYS.meetings] })
    },
  })
}

// 删除会议
export function useDeleteMeeting() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deleteMeeting,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [QUERY_KEYS.meetings] })
    },
  })
}

// 转写记录
export function useTranscripts(meetingId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.transcripts(meetingId || ''),
    queryFn: () => getTranscripts(meetingId!),
    enabled: !!meetingId,
  })
}

// 转写状态轮询（转写中时自动刷新）
export function useTranscriptionStatus(meetingId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.transcriptionStatus(meetingId || ''),
    queryFn: () => getTranscriptionStatus(meetingId!),
    enabled: !!meetingId,
    refetchInterval: (query) => {
      // 转写中时每 3 秒轮询
      const status = query.state.data?.status
      if (status === 'transcribing' || status === 'pending') {
        return 3000
      }
      return false
    },
  })
}
