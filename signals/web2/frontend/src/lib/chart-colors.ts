import { cssVar } from './api'

/** 从 CSS 变量读取当前主题的图表配色 */
export function chartColors() {
  return {
    bg: cssVar('--c-chart-bg') || '#0c0e18',
    grid: cssVar('--c-chart-grid') || '#141824',
    text: cssVar('--c-text-secondary') || '#7a8098',
    crosshair: cssVar('--c-text-muted') || '#464d64',
    upColor: cssVar('--c-up') || '#e8384f',
    downColor: cssVar('--c-down') || '#2d8a6e',
    biUp: cssVar('--c-bi-up') || '#e8384f',
    biDown: cssVar('--c-bi-down') || '#2d8a6e',
    signalBuy: cssVar('--c-signal-buy') || '#e8a33e',
    signalSell: cssVar('--c-signal-sell') || '#8e44ad',
    volUp: cssVar('--c-vol-up') || 'rgba(232,56,79,0.45)',
    volDown: cssVar('--c-vol-down') || 'rgba(45,138,110,0.45)',
    zhongshu: cssVar('--c-zhongshu') || 'rgba(59,125,255,0.5)',
    macdDif: cssVar('--c-macd-dif') || '#e8a33e',
    macdDea: cssVar('--c-macd-dea') || '#3b7dff',
    macdBarUp: cssVar('--c-macd-bar-up') || 'rgba(232,56,79,0.65)',
    macdBarDown: cssVar('--c-macd-bar-down') || 'rgba(45,138,110,0.65)',
  }
}
