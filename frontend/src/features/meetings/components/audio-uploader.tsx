import { useState, type DragEvent } from 'react'
import { UploadCloud, FileAudio, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface AudioUploaderProps {
  onFileSelect: (file: File | null) => void
  selectedFile: File | null
  uploadProgress?: number
  isUploading?: boolean
}

const ACCEPTED_TYPES = '.mp3,.wav,.m4a,.flac,.ogg,.webm'

export function AudioUploader({
  onFileSelect,
  selectedFile,
  uploadProgress,
  isUploading,
}: AudioUploaderProps) {
  const [isDragging, setIsDragging] = useState(false)

  const handleDragOver = (e: DragEvent) => {
    e.preventDefault()
    setIsDragging(true)
  }

  const handleDragLeave = (e: DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
  }

  const handleDrop = (e: DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
    const file = e.dataTransfer.files[0]
    if (file) {
      onFileSelect(file)
    }
  }

  const handleFileInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      onFileSelect(file)
    }
  }

  if (selectedFile) {
    return (
      <div className="rounded-lg border bg-card p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10">
              <FileAudio className="h-5 w-5 text-primary" />
            </div>
            <div>
              <p className="text-sm font-medium">{selectedFile.name}</p>
              <p className="text-xs text-muted-foreground">
                {(selectedFile.size / 1024 / 1024).toFixed(2)} MB
              </p>
            </div>
          </div>
          {!isUploading && (
            <Button
              variant="ghost"
              size="icon"
              onClick={() => onFileSelect(null)}
            >
              <X className="h-4 w-4" />
            </Button>
          )}
        </div>

        {isUploading && uploadProgress !== undefined && (
          <div className="mt-3">
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>上传中...</span>
              <span>{uploadProgress}%</span>
            </div>
            <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary transition-all"
                style={{ width: `${uploadProgress}%` }}
              />
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <label
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      className={cn(
        'flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed py-12 transition-colors',
        isDragging
          ? 'border-primary bg-primary/5'
          : 'border-input hover:border-primary/50 hover:bg-accent/50',
      )}
    >
      <UploadCloud className="h-10 w-10 text-muted-foreground" />
      <p className="mt-3 text-sm font-medium">拖拽音频文件到此处</p>
      <p className="mt-1 text-xs text-muted-foreground">或点击选择文件</p>
      <p className="mt-2 text-xs text-muted-foreground/70">支持 {ACCEPTED_TYPES}</p>
      <input
        type="file"
        accept={ACCEPTED_TYPES}
        className="hidden"
        onChange={handleFileInput}
      />
    </label>
  )
}
