import { create } from 'zustand'

interface ToastState {
  message: string
  visible: boolean
  show: (msg: string, duration?: number) => void
}

export const useToast = create<ToastState>((set) => ({
  message: '',
  visible: false,
  show: (msg, duration = 3000) => {
    set({ message: msg, visible: true })
    setTimeout(() => set({ visible: false }), duration)
  },
}))

export default function Toast() {
  const { message, visible } = useToast()

  if (!visible && !message) return null

  return (
    <div
      className={`fixed bottom-6 right-6 z-[999] rounded-lg border border-border bg-bg-tertiary px-5 py-3 text-sm text-text-primary shadow-lg transition-all duration-300 ${
        visible ? 'translate-x-0 opacity-100' : 'translate-x-10 opacity-0'
      }`}
    >
      {message}
    </div>
  )
}

/** 便捷 hook */
export function useShowToast() {
  return useToast((s) => s.show)
}
