export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api'
export const SSE_BASE_URL = import.meta.env.VITE_SSE_BASE_URL || '/api'

export const QUERY_KEYS = {
  // meetings
  meetings: 'meetings',
  meeting: (id: string) => ['meeting', id] as const,
  transcripts: (meetingId: string) => ['transcripts', meetingId] as const,
  transcriptionStatus: (meetingId: string) => ['transcription-status', meetingId] as const,
  // summaries
  summaries: 'summaries',
  summaryList: (page: number, pageSize: number) => ['summary-list', page, pageSize] as const,
  meetingSummary: (meetingId: string) => ['meeting-summary', meetingId] as const,
  summaryDetail: (meetingId: string) => ['summary-detail', meetingId] as const,
  actionItems: (meetingId: string) => ['action-items', meetingId] as const,
  risks: (meetingId: string) => ['risks', meetingId] as const,
  // chat
  chatSessions: (meetingId: string) => ['chat-sessions', meetingId] as const,
  chatMessages: (sessionId: string) => ['chat-messages', sessionId] as const,
  // knowledge
  knowledge: 'knowledge',
  knowledgeDocuments: (page: number, pageSize: number) => ['knowledge-documents', page, pageSize] as const,
  // agent runs
  agentRuns: 'agent-runs',
  agentRunsList: (params: Record<string, unknown>) => ['agent-runs-list', params] as const,
  agentRun: (id: string) => ['agent-run', id] as const,
  agentRunStats: 'agent-run-stats',
  tools: 'tools',
  // decisions
  decisions: 'decisions',
  decisionList: (skip: number, limit: number, meetingId?: string) =>
    ['decision-list', skip, limit, meetingId] as const,
  decision: (id: string) => ['decision', id] as const,
  decisionSearch: (query: string) => ['decision-search', query] as const,
  // notes
  note: (id: string) => ['note', id] as const,
  currentNote: (lectureId: string) => ['current-note', lectureId] as const,
} as const

export const APP_TITLE = '课堂学习助手'
