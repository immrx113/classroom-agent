import {
  useQuery,
  useMutation,
  useQueryClient,
} from '@tanstack/react-query'
import {
  listKnowledgeDocuments,
  deleteKnowledgeDocument,
  searchKnowledge,
  uploadDocument,
} from '@/api/knowledge'
import { QUERY_KEYS } from '@/lib/constants'

// 知识文档列表
export function useKnowledgeDocuments(page = 1, pageSize = 20) {
  return useQuery({
    queryKey: QUERY_KEYS.knowledgeDocuments(page, pageSize),
    queryFn: () => listKnowledgeDocuments(page, pageSize),
  })
}

// 删除文档
export function useDeleteKnowledgeDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: deleteKnowledgeDocument,
    onSuccess: () => {
      // 失效所有 knowledge 相关查询（列表 + 搜索结果）
      queryClient.invalidateQueries({ queryKey: ['knowledge-documents'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge'] })
    },
    onError: (err) => {
      console.error('删除文档失败:', err)
    },
  })
}

// 知识检索
export function useKnowledgeSearch() {
  return useMutation({
    mutationFn: ({ query, topK }: { query: string; topK?: number }) =>
      searchKnowledge(query, topK),
  })
}

// 上传文档
export function useUploadDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ file, onProgress }: { file: File; onProgress?: (p: number) => void }) =>
      uploadDocument(file, onProgress),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: [QUERY_KEYS.knowledge] })
    },
  })
}
