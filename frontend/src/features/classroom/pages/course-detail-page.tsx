import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, Plus, Loader2, Clock, PlayCircle } from 'lucide-react'
import { listCourses, listLectures } from '@/api/courses'
import { createLecture } from '../api'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import type { Course, LectureBrief } from '@/types'

export default function CourseDetailPage() {
  const { courseId = '' } = useParams()
  const [course, setCourse] = useState<Course | null>(null)
  const [lectures, setLectures] = useState<LectureBrief[]>([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [lectureDate, setLectureDate] = useState('')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const [cs, ls] = await Promise.all([listCourses(), listLectures(courseId)])
      setCourse(cs.find((c) => c.id === courseId) ?? null)
      setLectures(ls)
    } catch (e) {
      console.error('加载课时失败:', e)
      setError(e instanceof Error ? e.message : '加载课时失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [courseId])

  const handleCreate = async () => {
    if (!title.trim()) return
    setError(null)
    setCreating(true)
    try {
      await createLecture(courseId, {
        title: title.trim(),
        description: description.trim() || undefined,
        lecture_date: lectureDate || undefined,
      })
      setTitle('')
      setDescription('')
      setLectureDate('')
      setShowCreate(false)
      await load()
    } catch (e) {
      console.error('创建课时失败:', e)
      setError(e instanceof Error ? e.message : '创建课时失败')
    } finally {
      setCreating(false)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Link
            to="/courses"
            className="mb-2 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="h-4 w-4" />
            返回课程
          </Link>
          <h1 className="text-2xl font-bold">{course?.title ?? '课程'} · 课时</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            共 {lectures.length} 个课时，点击课时上传录音与课件
          </p>
        </div>
        <Button onClick={() => setShowCreate((v) => !v)}>
          <Plus className="h-4 w-4" />
          新建课时
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
                <Label htmlFor="lecture-title">课时标题</Label>
                <Input
                  id="lecture-title"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="如：第 1 讲 线性表"
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="lecture-date">日期（可选）</Label>
                <Input
                  id="lecture-date"
                  type="date"
                  value={lectureDate}
                  onChange={(e) => setLectureDate(e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="lecture-desc">描述（可选）</Label>
                <Input
                  id="lecture-desc"
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="简要说明"
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
        <div className="space-y-3">
          {[...Array(3)].map((_, i) => (
            <Skeleton key={i} className="h-16" />
          ))}
        </div>
      ) : lectures.length > 0 ? (
        <div className="space-y-3">
          {lectures.map((l) => (
            <Link key={l.id} to={`/courses/${courseId}/lectures/${l.id}`}>
              <Card className="transition-colors hover:border-primary">
                <CardContent className="flex items-center justify-between p-4">
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
                      <PlayCircle className="h-5 w-5 text-primary" />
                    </div>
                    <div>
                      <p className="text-sm font-medium">{l.title}</p>
                      <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                        <Badge variant="secondary">{l.status}</Badge>
                      </div>
                    </div>
                  </div>
                  <Clock className="h-4 w-4 text-muted-foreground" />
                </CardContent>
              </Card>
            </Link>
          ))}
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
          <Clock className="h-12 w-12 opacity-30" />
          <p className="mt-3 text-sm">该课程暂无课时，点击右上角「新建课时」开始</p>
        </div>
      )}
    </div>
  )
}
