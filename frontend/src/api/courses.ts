import { apiClient } from './client'
import type { Course, LectureBrief } from '@/types'

// 课程列表（可访问，未归档）
export async function listCourses(): Promise<Course[]> {
  const res = await apiClient
    .get('courses', { searchParams: { page: 1, page_size: 100 } })
    .json<{ items: Course[] }>()
  return res.items
}

// 课程下课时列表（范围选择）
export async function listLectures(courseId: string): Promise<LectureBrief[]> {
  return apiClient.get(`courses/${courseId}/lectures`).json()
}
