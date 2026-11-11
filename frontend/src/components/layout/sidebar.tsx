import { NavLink } from 'react-router-dom'
import {
  CalendarDays,
  FileText,
  MessageSquare,
  BookOpen,
  Activity,
  GitBranch,
  PanelLeftClose,
  PanelLeft,
  Sparkles,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useUIStore } from '@/stores/ui-store'
import { APP_TITLE } from '@/lib/constants'

const navItems = [
  { to: '/courses', label: '课程管理', icon: BookOpen },
  { to: '/classroom-qa', label: '课堂问答', icon: Sparkles },
  { to: '/retrieval', label: '课堂检索', icon: BookOpen },
  { to: '/summaries', label: '课堂摘要', icon: FileText },
  { to: '/decisions', label: '课堂讨论', icon: GitBranch },
  { to: '/chat', label: '学习对话', icon: MessageSquare },
  { to: '/agent-runs', label: '处理记录', icon: Activity },
  { to: '/meetings', label: '旧会议（兼容）', icon: CalendarDays },
]

export function Sidebar() {
  const { sidebarCollapsed, toggleSidebar } = useUIStore()

  return (
    <aside
      className={cn(
        'flex flex-col border-r bg-card transition-all duration-300',
        sidebarCollapsed ? 'w-16' : 'w-60',
      )}
    >
      {/* Logo 区域 */}
      <div className="flex h-16 items-center gap-2 border-b px-4">
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground text-sm font-bold">
          AI
        </div>
        {!sidebarCollapsed && <span className="font-semibold truncate">{APP_TITLE}</span>}
      </div>

      {/* 导航菜单 */}
      <nav className="flex-1 space-y-1 p-2">
        {navItems.map((item) => {
          const Icon = item.icon
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-primary text-primary-foreground'
                    : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                )
              }
            >
              <Icon className="h-5 w-5 shrink-0" />
              {!sidebarCollapsed && <span>{item.label}</span>}
            </NavLink>
          )
        })}
      </nav>

      {/* 折叠按钮 */}
      <div className="border-t p-2">
        <button
          onClick={toggleSidebar}
          className="flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-accent"
        >
          {sidebarCollapsed ? (
            <PanelLeft className="h-5 w-5" />
          ) : (
            <PanelLeftClose className="h-5 w-5" />
          )}
          {!sidebarCollapsed && <span>收起侧栏</span>}
        </button>
      </div>
    </aside>
  )
}
