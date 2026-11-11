import { create } from 'zustand'

interface UIState {
  // 侧边栏折叠状态
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  setSidebarCollapsed: (collapsed: boolean) => void

  // 暗色模式
  darkMode: boolean
  toggleDarkMode: () => void
  setDarkMode: (dark: boolean) => void
}

export const useUIStore = create<UIState>((set) => ({
  sidebarCollapsed: false,
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
  setSidebarCollapsed: (collapsed) => set({ sidebarCollapsed: collapsed }),

  darkMode: false,
  toggleDarkMode: () =>
    set((state) => {
      const dark = !state.darkMode
      document.documentElement.classList.toggle('dark', dark)
      return { darkMode: dark }
    }),
  setDarkMode: (dark) => {
    document.documentElement.classList.toggle('dark', dark)
    set({ darkMode: dark })
  },
}))
