import { Suspense } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from '@/lib/query-client'
import { AppRouter } from '@/router'
import './index.css'

// 页面加载占位
function PageLoading() {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <Suspense fallback={<PageLoading />}>
        <AppRouter />
      </Suspense>
    </QueryClientProvider>
  )
}
