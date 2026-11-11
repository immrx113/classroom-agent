import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { AlertTriangle, ShieldAlert, ShieldCheck } from 'lucide-react'
import type { Risk } from '@/types'

const severityConfig = {
  high: { label: '高', variant: 'destructive' as const, icon: ShieldAlert },
  medium: { label: '中', variant: 'warning' as const, icon: AlertTriangle },
  low: { label: '低', variant: 'secondary' as const, icon: ShieldCheck },
}

export function RiskList({ risks }: { risks: Risk[] }) {
  if (risks.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
        <ShieldCheck className="h-8 w-8 opacity-50" />
        <p className="mt-2 text-sm">暂无需要关注的学习事项</p>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {risks.map((risk) => {
        const config = severityConfig[risk.severity as keyof typeof severityConfig] || severityConfig.medium
        const Icon = config.icon

        return (
          <Card key={risk.id}>
            <CardContent className="p-4">
              <div className="flex items-start gap-3">
                <div className="mt-0.5 shrink-0">
                  <Icon className="h-5 w-5 text-muted-foreground" />
                </div>
                <div className="flex-1 space-y-2">
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-sm font-medium">{risk.description}</p>
                    <Badge variant={config.variant} className="shrink-0">
                      {config.label}
                    </Badge>
                  </div>
                  {risk.mitigation && (
                    <div className="rounded-md bg-muted p-2 text-xs">
                      <span className="font-medium">缓解措施：</span>
                      {risk.mitigation}
                    </div>
                  )}
                </div>
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
