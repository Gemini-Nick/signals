import { useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { useShowToast } from '@/components/Toast'

interface ClusterMember {
  name: string; gain_pct: number; leader?: string; type?: string
}
interface Cluster {
  label: string; score: number; avg_gain: number; avg_breadth: number
  avg_turnover: number; size: number; members: ClusterMember[]
}
interface ClusterData {
  top: Cluster[]; meta?: { date?: string; source?: string; total_boards?: number; n_clusters?: number }
}

export default function ClusterPage() {
  const [industry, setIndustry] = useState<ClusterData | null>(null)
  const [concept, setConcept] = useState<ClusterData | null>(null)
  const [loading, setLoading] = useState(true)
  const toast = useShowToast()

  const load = async () => {
    setLoading(true)
    try {
      const data = await apiFetch<{ industry: ClusterData; concept: ClusterData }>('/api/cluster/latest?top=3')
      setIndustry(data.industry || data as unknown as ClusterData)
      setConcept(data.concept)
    } catch (e: unknown) {
      toast('加载失败: ' + (e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const refresh = async () => {
    try {
      await apiFetch('/api/cluster/refresh')
      toast('刷新成功')
      load()
    } catch (e: unknown) {
      toast('刷新失败: ' + (e as Error).message)
    }
  }

  useEffect(() => { load() }, [])

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center justify-between mb-6">
        <h2 className="font-display text-xl font-bold">行业板块聚类分析</h2>
        <div className="flex items-center gap-3">
          {industry?.meta && (
            <span className="text-xs font-mono text-text-muted">
              {industry.meta.date} | {industry.meta.source} | {industry.meta.total_boards}板块
            </span>
          )}
          <button onClick={refresh} className="rounded-md border border-border bg-bg-tertiary px-3 py-1.5 text-xs text-text-primary hover:border-text-muted transition cursor-pointer">
            刷新
          </button>
        </div>
      </div>

      {loading ? (
        <div className="text-center py-20 text-text-muted font-display">加载中...</div>
      ) : (
        <>
          {/* 行业板块 */}
          <SectionLabel label="行业板块" source={industry?.meta?.source || '东财'} />
          {industry?.top && <ClusterGrid clusters={industry.top} />}

          {/* 概念板块 */}
          <SectionLabel label="概念板块" source="THS" />
          {concept?.top?.length ? (
            <ClusterGrid clusters={concept.top} />
          ) : (
            <div className="text-sm text-text-muted py-8 text-center">概念聚类数据加载中...</div>
          )}
        </>
      )}
    </div>
  )
}

function SectionLabel({ label, source }: { label: string; source: string }) {
  return (
    <div className="flex items-center gap-2 mt-5 mb-3 text-sm font-semibold text-text-secondary">
      {label}
      <span className="text-[11px] font-medium text-text-secondary bg-bg-tertiary px-2 py-0.5 rounded-full border border-border">
        {source}
      </span>
    </div>
  )
}

function ClusterGrid({ clusters }: { clusters: Cluster[] }) {
  return (
    <div className="grid grid-cols-2 gap-5 max-md:grid-cols-1 [&>*:first-child]:col-span-2 max-md:[&>*:first-child]:col-span-1">
      {clusters.map((c, i) => (
        <ClusterCard key={c.label} cluster={c} rank={i + 1} />
      ))}
    </div>
  )
}

function ClusterCard({ cluster: c, rank }: { cluster: Cluster; rank: number }) {
  const [open, setOpen] = useState(false)
  const gainCls = c.avg_gain >= 0 ? 'text-up' : 'text-down'

  return (
    <div className="rounded-xl border border-border bg-bg-secondary p-5 transition-all duration-200 hover:-translate-y-0.5 hover:border-accent hover:shadow-[var(--c-glow)]">
      {/* Header */}
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-3">
          <span className={`font-display text-2xl font-extrabold ${rank === 1 ? 'text-accent-gold' : 'text-accent'}`}
            style={rank === 1 ? { textShadow: '0 0 12px rgba(196,162,78,0.3)' } : undefined}>
            #{rank}
          </span>
          <span className="font-display text-base font-bold text-text-primary">{c.label}</span>
        </div>
        <span className="text-xs font-mono text-text-muted">综合 {(c.score * 100).toFixed(0)}</span>
      </div>

      {/* Metrics */}
      <div className="flex gap-4 flex-wrap mb-3 text-xs text-text-secondary">
        <span><span className={`font-mono font-semibold ${gainCls}`}>{c.avg_gain >= 0 ? '+' : ''}{c.avg_gain}%</span> 均涨幅</span>
        <span><span className="font-mono font-semibold text-text-primary">{(c.avg_breadth * 100).toFixed(0)}%</span> 广度</span>
        <span><span className="font-mono font-semibold text-text-primary">{c.avg_turnover}%</span> 换手</span>
        <span><span className="font-mono font-semibold text-text-primary">{c.size}</span> 板块</span>
      </div>

      {/* Members Toggle */}
      <button onClick={() => setOpen(!open)} className="text-xs text-text-muted hover:text-text-primary cursor-pointer transition">
        {open ? '▼' : '▶'} 成员板块 ({c.members.length})
      </button>
      {open && (
        <div className="mt-2 space-y-0.5">
          {c.members.map((m) => (
            <div key={m.name} className="flex items-center justify-between py-1 text-xs border-b border-white/[0.025] last:border-0 hover:bg-bg-tertiary/30 transition">
              <span className="text-text-primary">{m.name}</span>
              <span className={`font-mono font-semibold ${m.gain_pct >= 0 ? 'text-up' : 'text-down'}`}>
                {m.gain_pct >= 0 ? '+' : ''}{m.gain_pct}%
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
