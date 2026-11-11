import { Moon, Sun, Bell, Search } from 'lucide-react'
import { useUIStore } from '@/stores/ui-store'
import { Button } from '@/components/ui/button'

export function Header() {
  const { darkMode, toggleDarkMode } = useUIStore()

  return (
    <header className="flex h-16 items-center justify-between border-b bg-card px-6">
      {/* 搜索栏 */}
      <div className="flex flex-1 items-center gap-4">
        <div className="relative w-full max-w-md">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <input
            type="text"
            placeholder="搜索课时、笔记、学习资料..."
            className="w-full rounded-md border border-input bg-background py-2 pl-10 pr-4 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </div>
      </div>

      {/* 操作区 */}
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="icon" onClick={toggleDarkMode} title="切换主题">
          {darkMode ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
        </Button>
        <Button variant="ghost" size="icon" title="通知">
          <Bell className="h-5 w-5" />
        </Button>
        <div className="ml-2 flex h-8 w-8 items-center justify-center rounded-full bg-primary text-sm font-medium text-primary-foreground">
          U
        </div>
      </div>
    </header>
  )
}
