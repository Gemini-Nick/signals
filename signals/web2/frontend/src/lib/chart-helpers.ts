/**
 * Lightweight Charts v4 类型适配
 *
 * 后端返回 Unix timestamp (number)，LWC v4 需要 UTCTimestamp 类型。
 * 用 `as UTCTimestamp` 做类型断言，运行时值不变。
 */
import type {
  UTCTimestamp,
  CandlestickData,
  HistogramData,
  LineData,
  SeriesMarker,
} from 'lightweight-charts'
import { ColorType } from 'lightweight-charts'

export { ColorType }
export type { UTCTimestamp }

/** OHLCV bar from API → LWC CandlestickData */
export interface ApiBar {
  time: number; open: number; high: number; low: number; close: number; volume: number
}

export function toCandleData(bars: ApiBar[]): CandlestickData<UTCTimestamp>[] {
  return bars.map(b => ({
    time: b.time as UTCTimestamp,
    open: b.open, high: b.high, low: b.low, close: b.close,
  }))
}

export function toVolumeData(bars: ApiBar[], upColor: string, downColor: string): HistogramData<UTCTimestamp>[] {
  return bars.map(b => ({
    time: b.time as UTCTimestamp,
    value: b.volume,
    color: b.close >= b.open ? upColor : downColor,
  }))
}

export function toLineData(data: { time: number; value: number }[]): LineData<UTCTimestamp>[] {
  return data.map(d => ({ time: d.time as UTCTimestamp, value: d.value }))
}

export function toHistogramData(data: { time: number; value: number; color: string }[]): HistogramData<UTCTimestamp>[] {
  return data.map(d => ({ time: d.time as UTCTimestamp, value: d.value, color: d.color }))
}

export function toMarkers(markers: { time: number; position: 'aboveBar' | 'belowBar'; color: string; shape: 'arrowUp' | 'arrowDown'; text: string }[]): SeriesMarker<UTCTimestamp>[] {
  return markers.map(m => ({ ...m, time: m.time as UTCTimestamp }))
}
