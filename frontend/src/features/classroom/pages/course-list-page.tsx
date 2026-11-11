import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Plus, BookOpen, Loader2, GraduationCap, CalendarDays } from 'lucide-react'
import { listCourses } from '@/api/courses'
import { createCourse } from '../api'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import type { Course } from '@/types'

export default function CourseListPage() {
  const [courses, setCourses] = useState<Course[]>([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [title, setTitle] = useState('')
  const [teacher, setTeacher] = useState('')
  const [term, setTerm] = useState('')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      setCourses(await listCourses())
    } catch (e) {
      console.error('加载课程失败:', e)
      setError(e instanceof Error ? e.message : '加载课程失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const handleCreate = async () => {
    if (!title.trim()) return
    setError(null)
    setCreating(true)
    try {
      await createCourse({
        title: title.trim(),
        teacher: teacher.trim() || undefined,
        term: term.trim() || undefined,
      })
      setTitle('')
      setTeacher('')
      setTerm('')
      setShowCreate(false)
      await load()
    } catch (e) {
      console.error('创建课程失败:', e)
      setError(e instanceof Error ? e.message : '创建课程失败')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">课程管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            创建课程与课时，上传录音与课件作为课堂学习资料
          </p>
        </div>
        <Button onClick={() => setShowCreate((v) => !v)}>
          <Plus className="h-4 w-4" />
          新建课程
        </Button>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 px-4 py-2 text-sm text-red-700 dark:bg-red-950 dark:text-red-400">
          ✗ {error}
        </div>
      )}

      {showCreate && (
        <Card>
          <CardContent className="space-y-4 p-4">
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="flex flex-col gap-1">
                <Label htmlFor="course-title">课程名称</Label>
                <Input
                  id="course-title"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="如：数据结构"
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="course-teacher">授课教师（可选）</Label>
                <Input
                  id="course-teacher"
                  value={teacher}
                  onChange={(e) => setTeacher(e.target.value)}
                  placeholder="如：张老师"
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="course-term">学期（可选）</Label>
                <Input
                  id="course-term"
                  value={term}
                  onChange={(e) => setTerm(e.target.value)}
                  placeholder="如：2026 秋"
                />
              </div>
            </div>
            <div className="flex gap-2">
              <Button onClick={handleCreate} disabled={creating || !title.trim()}>
                {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                创建
              </Button>
              <Button variant="ghost" onClick={() => setShowCreate(false)}>
                取消
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {loading ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[...Array(3)].map((_, i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
      ) : courses.length > 0 ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {courses.map((c) => (
            <Link key={c.id} to={`/courses/${c.id}`}>
              <Card className="h-full transition-colors hover:border-primary">
                <CardContent className="p-4">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10">
                        <BookOpen className="h-5 w-5 text-primary" />
                      </div>
                      <span className="font-medium">{c.title}</span>
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {c.teacher && (
                      <Badge variant="secondary">
                        <GraduationCap className="mr-1 h-3 w-3" />
                        {c.teacher}
                      </Badge>
                    )}
                    {c.term && <Badge variant="outline">{c.term}</Badge>}
                    <Badge variant="info">
                      <CalendarDays className="mr-1 h-3 w-3" />
                      {c.lecture_count ?? 0} 课时
                    </Badge>
                  </div>
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <BookOpen className="h-12 w-12 opacity-30" />
          <p className="mt-3 text-sm">暂无课程，点击右上角「新建课程」开始</p>
        </div>
      )}
    </div>
  )
}
