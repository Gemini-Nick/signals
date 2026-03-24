import { useRef, useState, useCallback } from 'react'
import { createChart, type IChartApi } from 'lightweight-charts'
import { apiFetch } from '@/lib/api'
import { chartColors } from '@/lib/chart-colors'
import { useShowToast } from '@/components/Toast'
import { ColorType, toCandleData, toVolumeData, toHistogramData, toMarkers, type ApiBar } from '@/lib/chart-helpers'

interface Signal {
  dt: number; date_str: string; type: string; group: string; price: number
  confidence: number | null
  eval?: { return_t5?: number; return_t10?: number; return_t20?: number; mfe?: number; mae?: number }
}
interface KPI {
  total: number; evaluated?: number; win_rate: number; expectancy: number
  avg_return_t10: number; avg_mfe?: number; avg_mae?: number
  by_type?: Record<string, { count: number; win_rate: number; avg_return_t10: number }>
}

export default function BacktestPage() {
  const [code, setCode] = useState('')
  const [freq, setFreq] = useState('daily')
  const [group, setGroup] = useState('all')
  const [loading, setLoading] = useState(false)
  const [kpi, setKpi] = useState<KPI | null>(null)
  const [signals, setSignals] = useState<Signal[]>([])
  const chartRef = useRef<HTMLDivElement>(null)
  const chartApi = useRef<IChartApi | null>(null)
  const toast = useShowToast()

  const run = useCallback(async () => {
    if (!code.trim()) return
    setLoading(true)
    try {
      const params = new URLSearchParams({ code: code.trim(), freq, signal_group: group })
      const data = await apiFetch<Record<string, unknown>>('/api/backtest/run?' + params)
      if ((data as { error?: string }).error) { toast((data as { error: string }).error); return }
      toast(`${data.symbol} ${data.freq} — ${(data.signals as unknown[]).length} 信号`)
      renderChart(data)
      setKpi(data.kpi as KPI)
      setSignals(data.signals as Signal[])
    } catch (e: unknown) {
      toast('失败: ' + (e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [code, freq, group, toast])

  const renderChart = (data: Record<string, unknown>) => {
    const container = chartRef.current
    if (!container) return
    const c = chartColors()
    if (chartApi.current) { chartApi.current.remove(); chartApi.current = null }
    container.innerHTML = ''

    const chart = createChart(container, {
      width: container.clientWidth, height: 520,
      layout: { background: { type: ColorType.Solid, color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { vertLine: { color: c.crosshair, width: 1, style: 2 }, horzLine: { color: c.crosshair, width: 1, style: 2 } },
      timeScale: { borderColor: c.grid }, rightPriceScale: { borderColor: c.grid },
    })
    chartApi.current = chart

    const ohlcv = data.ohlcv as ApiBar[]
    if (ohlcv?.length) {
      const candle = chart.addCandlestickSeries({ upColor: c.upColor, downColor: c.downColor, borderUpColor: c.upColor, borderDownColor: c.downColor, wickUpColor: c.upColor, wickDownColor: c.downColor })
      candle.setData(toCandleData(ohlcv))
      const vol = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'volume' })
      chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.72, bottom: 0.18 } })
      vol.setData(toVolumeData(ohlcv, c.volUp, c.volDown))

      // 信号标记
      const sigs = data.signals as Signal[]
      if (sigs?.length) {
        const markers = sigs.map(s => {
          const isBuy = s.group === 'macd' || s.type.includes('买')
          return {
            time: s.dt,
            position: isBuy ? 'belowBar' as const : 'aboveBar' as const,
            color: isBuy ? c.signalBuy : c.signalSell,
            shape: isBuy ? 'arrowUp' as const : 'arrowDown' as const,
            text: s.type,
          }
        }).sort((a, b) => a.time - b.time)
        candle.setMarkers(toMarkers(markers))
      }
    }

    // MACD
    const macd = data.macd as { time: number; dif: number; dea: number; bar: number }[]
    if (macd?.length >= 2) {
      const bar = chart.addHistogramSeries({ priceScaleId: 'macd', priceFormat: { type: 'price', precision: 4, minMove: 0.0001 }, lastValueVisible: false })
      chart.priceScale('macd').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } })
      bar.setData(toHistogramData(macd.map(d => ({ time: d.time, value: d.bar, color: d.bar >= 0 ? c.macdBarUp : c.macdBarDown }))))
    }

    chart.timeScale().fitContent()
    const ro = new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth }))
    ro.observe(container)
  }

  const scrollTo = (time: number) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    chartApi.current?.timeScale().setVisibleRange({ from: time - 60 * 86400, to: time + 30 * 86400 } as any)
  }

  return (
    <div>
      {/* Input Row */}
      <div className="flex flex-wrap gap-2 items-center mb-4">
        <input value={code} onChange={e => setCode(e.target.value)} onKeyDown={e => e.key === 'Enter' && run()}
          placeholder="股票代码 (如 002759)"
          className="rounded-md border border-border bg-bg-tertiary px-3 py-1.5 text-sm text-text-primary focus:border-accent focus:outline-none transition w-40" />
        <select value={freq} onChange={e => setFreq(e.target.value)}
          className="rounded-md border border-border bg-bg-tertiary px-3 py-1.5 text-sm text-text-primary">
          <option value="daily">日线</option>
          <option value="weekly">周线</option>
        </select>
        <select value={group} onChange={e => setGroup(e.target.value)}
          className="rounded-md border border-border bg-bg-tertiary px-3 py-1.5 text-sm text-text-primary">
          <option value="all">全部信号</option>
          <option value="macd">仅 MACD</option>
          <option value="czsc">仅缠论</option>
        </select>
        <button onClick={run} disabled={loading}
          className="rounded-md bg-accent px-4 py-1.5 text-sm font-medium text-white hover:bg-[var(--c-accent-hover)] disabled:opacity-50 transition cursor-pointer">
          {loading ? '加载中...' : '运行回测'}
        </button>
      </div>

      {/* KPI Cards */}
      {kpi && <KpiCards kpi={kpi} />}

      {/* Chart */}
      <div ref={chartRef} className="rounded-lg overflow-hidden" style={{ height: 520 }} />

      {/* Signal Table */}
      {signals.length > 0 && (
        <div className="mt-4 overflow-x-auto">
          <div className="text-sm font-display font-semibold text-text-secondary mb-2 pb-2 border-b border-border bg-[image:var(--c-spotlight)] bg-no-repeat bg-bottom bg-[length:100%_1px]">
            信号明细
          </div>
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="text-text-muted text-[11px] uppercase tracking-wider">
                <th className="py-2 px-2 text-left sticky top-0 bg-bg-primary">日期</th>
                <th className="py-2 px-2 text-left sticky top-0 bg-bg-primary">类型</th>
                <th className="py-2 px-2 text-left sticky top-0 bg-bg-primary">组</th>
                <th className="py-2 px-2 text-left sticky top-0 bg-bg-primary">价格</th>
                <th className="py-2 px-2 text-left sticky top-0 bg-bg-primary">T+5</th>
                <th className="py-2 px-2 text-left sticky top-0 bg-bg-primary">T+10</th>
                <th className="py-2 px-2 text-left sticky top-0 bg-bg-primary">T+20</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s, i) => {
                const ev = s.eval || {}
                const t10 = ev.return_t10
                const rowBg = t10 != null ? (t10 > 0 ? 'bg-down/5' : 'bg-up/5') : ''
                return (
                  <tr key={i} onClick={() => scrollTo(s.dt)}
                    className={`cursor-pointer border-b border-border hover:bg-bg-tertiary/50 transition ${rowBg}`}>
                    <td className="py-1.5 px-2">{s.date_str}</td>
                    <td className="py-1.5 px-2 font-semibold">{s.type}</td>
                    <td className="py-1.5 px-2">
                      <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold ${s.group === 'macd' ? 'bg-up/10 text-up' : 'bg-accent-blue/10 text-[var(--c-accent-blue)]'}`}>
                        {s.group === 'macd' ? 'MACD' : '缠论'}
                      </span>
                    </td>
                    <td className="py-1.5 px-2 font-mono">{s.price.toFixed(2)}</td>
                    <td className={`py-1.5 px-2 font-mono ${retCls(ev.return_t5)}`}>{fmtRet(ev.return_t5)}</td>
                    <td className={`py-1.5 px-2 font-mono font-semibold ${retCls(ev.return_t10)}`}>{fmtRet(ev.return_t10)}</td>
                    <td className={`py-1.5 px-2 font-mono ${retCls(ev.return_t20)}`}>{fmtRet(ev.return_t20)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function KpiCards({ kpi }: { kpi: KPI }) {
  const items = [
    { value: kpi.total, label: '总信号', cls: '' },
    { value: `${kpi.win_rate}%`, label: '胜率(T+10)', cls: kpi.win_rate >= 50 ? 'text-up' : 'text-down' },
    { value: `${kpi.expectancy >= 0 ? '+' : ''}${kpi.expectancy}%`, label: '期望收益', cls: kpi.expectancy >= 0 ? 'text-up' : 'text-down' },
    { value: `${kpi.avg_return_t10}%`, label: '平均T+10', cls: kpi.avg_return_t10 >= 0 ? 'text-up' : 'text-down' },
    { value: `+${kpi.avg_mfe || 0}%`, label: 'MFE均', cls: 'text-up' },
    { value: `${kpi.avg_mae || 0}%`, label: 'MAE均', cls: 'text-down' },
  ]
  return (
    <div className="flex flex-wrap gap-3 mb-4">
      {items.map(it => (
        <div key={it.label} className="rounded-lg border border-border bg-bg-secondary px-4 py-3 min-w-[100px] text-center hover:border-accent transition">
          <div className={`text-lg font-bold font-mono ${it.cls}`}>{it.value}</div>
          <div className="text-[11px] text-text-muted mt-0.5">{it.label}</div>
        </div>
      ))}
    </div>
  )
}

function fmtRet(val?: number) { return val == null ? '—' : `${val >= 0 ? '+' : ''}${val}%` }
function retCls(val?: number) { return val == null ? '' : val >= 0 ? 'text-up' : 'text-down' }
