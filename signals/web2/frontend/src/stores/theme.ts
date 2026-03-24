import { create } from 'zustand'

type Theme = 'bronze' | 'cinnabar'

interface ThemeStore {
  theme: Theme
  toggle: () => void
}

export const useTheme = create<ThemeStore>((set) => {
  // 初始化：读 localStorage，迁移旧名
  let saved = localStorage.getItem('web2-theme') || 'bronze'
  if (saved === 'tradingview') saved = 'bronze'
  if (saved === 'anthropic') saved = 'cinnabar'
  if (saved !== 'bronze' && saved !== 'cinnabar') saved = 'bronze'
  document.documentElement.setAttribute('data-theme', saved)

  return {
    theme: saved as Theme,
    toggle: () =>
      set((state) => {
        const next = state.theme === 'bronze' ? 'cinnabar' : 'bronze'
        document.documentElement.setAttribute('data-theme', next)
        localStorage.setItem('web2-theme', next)
        return { theme: next }
      }),
  }
})
