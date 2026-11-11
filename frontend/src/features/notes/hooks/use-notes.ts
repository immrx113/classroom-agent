import { useQuery } from '@tanstack/react-query'
import { getNote, getCurrentNote } from '../api'
import { QUERY_KEYS } from '@/lib/constants'

// 指定笔记
export function useNote(id: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.note(id || ''),
    queryFn: () => getNote(id!),
    enabled: !!id,
  })
}

// 课时当前笔记
export function useCurrentNote(lectureId: string | undefined) {
  return useQuery({
    queryKey: QUERY_KEYS.currentNote(lectureId || ''),
    queryFn: () => getCurrentNote(lectureId!),
    enabled: !!lectureId,
  })
}
