import { useRef, useState, useCallback } from 'react'
import { createChart, type IChartApi } from 'lightweight-charts'
import { apiFetch } from '@/lib/api'
import { chartColors } from '@/lib/chart-colors'
import { useShowToast } from '@/components/Toast'
import { ColorType, toCandleData, toVolumeData, toLineData, toHistogramData, toMarkers, type ApiBar } from '@/lib/chart-helpers'

const INDICES = ['沪深300', '上证指数', '创业板指', '上证50', '科创50', '中证500', '中证1000']
const FREQS = [
  { value: 'daily', label: '日线' },
  { value: '30min', label: '30分' },
  { value: '15min', label: '15分' },
]

export default function ChartPage() {
  const [symbol, setSymbol] = useState('')
  const [freq, setFreq] = useState('daily')
  const [report, setReport] = useState<Record<string, unknown> | null>(null)
  const [_signals, setSignals] = useState<unknown[]>([])
  const chartRef = useRef<HTMLDivElement>(null)
  const chartApi = useRef<IChartApi | null>(null)
  const toast = useShowToast()

  const loadChart = useCallback(async (name: string, f: string) => {
    setSymbol(name)
    setFreq(f)
    try {
      const data = await apiFetch<Record<string, unknown>>(
        `/api/chart/${encodeURIComponent(name)}?freq=${f}`
      )
      renderChart(data)
      setReport(data.report as Record<string, unknown>)
      setSignals(data.signals as unknown[])
    } catch (e: unknown) {
      toast('图表加载失败: ' + (e as Error).message)
    }
  }, [toast])

  const renderChart = (data: Record<string, unknown>) => {
    const container = chartRef.current
    if (!container) return
    const c = chartColors()

    // 清除旧图表
    if (chartApi.current) { chartApi.current.remove(); chartApi.current = null }
    container.innerHTML = ''

    const chart = createChart(container, {
      width: container.clientWidth,
      height: 560,
      layout: { background: { type: ColorType.Solid, color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: {
        vertLine: { color: c.crosshair, width: 1, style: 2 },
        horzLine: { color: c.crosshair, width: 1, style: 2 },
      },
      timeScale: { borderColor: c.grid, timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: c.grid },
    })
    chartApi.current = chart

    // K线
    const candle = chart.addCandlestickSeries({
      upColor: c.upColor, downColor: c.downColor,
      borderUpColor: c.upColor, borderDownColor: c.downColor,
      wickUpColor: c.upColor, wickDownColor: c.downColor,
    })

    const ohlcv = data.ohlcv as ApiBar[]
    if (ohlcv?.length) {
      candle.setData(toCandleData(ohlcv))
      const vol = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'volume' })
      chart.priceScale('volume').applyOptions({ scaleMargins: { top: 0.72, bottom: 0.18 } })
      vol.setData(toVolumeData(ohlcv, c.volUp, c.volDown))
    }

    // 笔线
    const biList = data.bi_list as { sdt: number; edt: number; high: number; low: number; direction: string }[]
    if (biList?.length) {
      const points: { time: number; value: number }[] = []
      biList.forEach(bi => {
        if (bi.direction === 'up') {
          points.push({ time: bi.sdt, value: bi.low }, { time: bi.edt, value: bi.high })
        } else {
          points.push({ time: bi.sdt, value: bi.high }, { time: bi.edt, value: bi.low })
        }
      })
      const merged = dedup(points)
      if (merged.length >= 2) {
        const bi = chart.addLineSeries({ color: c.biUp, lineWidth: 2, crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false })
        bi.setData(toLineData(merged))
      }
    }

    // MA 均线
    const maLines = data.ma_lines as { label: string; color: string; data: { time: number; value: number }[] }[]
    maLines?.forEach(ma => {
      if (ma.data?.length >= 2) {
        const s = chart.addLineSeries({ color: ma.color, lineWidth: 1, crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false, priceScaleId: '' })
        s.setData(toLineData(ma.data))
      }
    })

    // MACD
    const macd = data.macd as { time: number; dif: number; dea: number; bar: number }[]
    if (macd?.length >= 2) {
      const bar = chart.addHistogramSeries({ priceScaleId: 'macd', priceFormat: { type: 'price', precision: 4, minMove: 0.0001 }, lastValueVisible: false })
      chart.priceScale('macd').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } })
      bar.setData(toHistogramData(macd.map(d => ({ time: d.time, value: d.bar, color: d.bar >= 0 ? c.macdBarUp : c.macdBarDown }))))
      const dif = chart.addLineSeries({ color: c.macdDif, lineWidth: 1, priceScaleId: 'macd', crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false })
      dif.setData(toLineData(macd.map(d => ({ time: d.time, value: d.dif }))))
      const dea = chart.addLineSeries({ color: c.macdDea, lineWidth: 1, priceScaleId: 'macd', crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false })
      dea.setData(toLineData(macd.map(d => ({ time: d.time, value: d.dea }))))
    }

    // 信号标记
    const sigs = data.signals as { dt: number; type: string }[]
    if (sigs?.length) {
      const markers = sigs.map(s => {
        const isBuy = s.type.includes('买')
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

    chart.timeScale().fitContent()

    // Resize
    const ro = new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth }))
    ro.observe(container)
    return () => ro.disconnect()
  }

  return (
    <div>
      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-4 mb-4">
        <div>
          <h2 className="font-display text-xl font-bold mb-2">{symbol || '选择指数'}</h2>
          <div className="flex flex-wrap gap-1">
            {INDICES.map(name => (
              <button key={name} onClick={() => loadChart(name, freq)}
                className={`rounded-md border px-2.5 py-1 text-xs cursor-pointer transition-all ${
                  symbol === name ? 'border-accent bg-bg-tertiary text-text-primary' : 'border-border text-text-secondary hover:text-text-primary hover:border-text-muted'
                }`}>
                {name}
              </button>
            ))}
          </div>
        </div>
        <div className="flex gap-1">
          {FREQS.map(f => (
            <button key={f.value} onClick={() => symbol && loadChart(symbol, f.value)}
              className={`rounded-md border px-3 py-1 text-xs cursor-pointer transition-all ${
                freq === f.value ? 'border-accent bg-accent text-white' : 'border-border text-text-secondary hover:text-text-primary'
              }`}>
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {/* Chart */}
      <div ref={chartRef} className="rounded-lg overflow-hidden" style={{ height: 560 }} />

      {/* Signal Details */}
      {report && (
        <div className="mt-4 rounded-xl border border-border bg-bg-secondary p-4 backdrop-blur-sm">
          {(report.conclusion as string) && (
            <div className="text-sm font-display font-semibold text-accent-gold mb-3 p-2 rounded-md border-l-3 border-accent-gold"
              style={{ background: 'var(--c-bg-tertiary)' }}>
              {report.conclusion as string}
            </div>
          )}
          {(report.daily_trend as string) && (
            <div className="flex gap-2 flex-wrap text-xs mb-2">
              <TrendChip label="日线" value={report.daily_trend as string} />
              <TrendChip label="30M" value={report.f30_trend as string} />
              <TrendChip label="15M" value={report.f15_trend as string} />
            </div>
          )}
          {(report.summary as string) && (
            <p className="text-xs text-text-secondary mt-2 leading-relaxed">{report.summary as string}</p>
          )}
        </div>
      )}
    </div>
  )
}

function TrendChip({ label, value }: { label: string; value: string }) {
  const cls = value === '上涨趋势' ? 'text-up bg-up/10' : value === '下跌趋势' ? 'text-down bg-down/10' : 'text-text-secondary bg-bg-tertiary'
  return <span className={`px-2 py-0.5 rounded text-xs font-medium ${cls}`}>{label}: {value}</span>
}

/** 去重 + 排序 + 合并相同时间点 */
function dedup(points: { time: number; value: number }[]) {
  const seen = new Set<string>()
  const unique = points.filter(p => {
    const k = `${p.time}_${p.value}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  }).sort((a, b) => a.time - b.time)

  const merged: typeof unique = []
  unique.forEach(p => {
    if (merged.length > 0 && merged[merged.length - 1].time === p.time) {
      merged[merged.length - 1] = p
    } else merged.push(p)
  })
  return merged
}
