import { apiClient } from '@/api/client'
import type { Note, NoteEvidence } from '@/types'

// 获取指定笔记（本课笔记阅读入口）
export async function getNote(noteId: string): Promise<Note> {
  return apiClient.get(`notes/${noteId}`).json()
}

// 获取课时当前笔记
export async function getCurrentNote(lectureId: string): Promise<Note | null> {
  return apiClient.get(`lectures/${lectureId}/notes/current`).json()
}

// 获取证据：回听定位（start_ms/end_ms）、原文、来源版本状态（is_current）
export async function getEvidence(evidenceId: string): Promise<NoteEvidence> {
  return apiClient.get(`evidence/${evidenceId}`).json()
}
