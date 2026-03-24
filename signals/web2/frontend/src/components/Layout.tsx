import { NavLink, Outlet } from 'react-router-dom'
import { useTheme } from '@/stores/theme'

const NAV_ITEMS = [
  { to: '/', icon: '📊', label: '行业聚类' },
  { to: '/chart', icon: '📈', label: '图表' },
  { to: '/backtest', icon: '⚡', label: '信号回测' },
]

export default function Layout() {
  const toggle = useTheme((s) => s.toggle)

  return (
    <div className="flex min-h-screen">
      {/* Nav Rail */}
      <nav className="group fixed top-0 left-0 z-50 flex h-screen w-14 flex-col items-center border-r border-border bg-bg-secondary py-3 transition-all duration-250 hover:w-44 overflow-hidden max-md:top-auto max-md:bottom-0 max-md:h-14 max-md:w-full max-md:flex-row max-md:border-r-0 max-md:border-t max-md:py-0 max-md:hover:w-full">
        {/* Logo */}
        <div className="font-display text-xl font-bold pb-5 text-text-primary max-md:hidden" style={{ textShadow: 'var(--c-glow)' }}>
          🐲
        </div>

        {/* Nav Items */}
        <div className="flex flex-1 flex-col gap-1 w-full px-2 max-md:flex-row max-md:justify-around max-md:px-0 max-md:gap-0">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) =>
                `relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium whitespace-nowrap transition-all duration-200 max-md:flex-col max-md:gap-0.5 max-md:px-1 max-md:py-1.5 max-md:text-[10px] max-md:items-center max-md:justify-center ${
                  isActive
                    ? 'bg-bg-tertiary text-text-primary'
                    : 'text-text-secondary hover:text-text-primary hover:bg-bg-tertiary/50'
                }`
              }
            >
              {({ isActive }) => (
                <>
                  {isActive && (
                    <span className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-6 bg-accent rounded-r max-md:hidden" />
                  )}
                  <span className="text-lg min-w-6 text-center shrink-0 max-md:text-xl">{item.icon}</span>
                  <span className="opacity-0 group-hover:opacity-100 transition-opacity duration-200 delay-100 max-md:opacity-100">
                    {item.label}
                  </span>
                </>
              )}
            </NavLink>
          ))}
        </div>

        {/* Theme Toggle */}
        <div className="w-full px-2 max-md:hidden">
          <button
            onClick={toggle}
            className="flex items-center gap-3 w-full rounded-lg px-3 py-2.5 text-sm text-text-secondary hover:text-text-primary hover:bg-bg-tertiary/50 transition-all cursor-pointer"
          >
            <span className="text-lg min-w-6 text-center">🎨</span>
            <span className="opacity-0 group-hover:opacity-100 transition-opacity duration-200 delay-100">
              切换主题
            </span>
          </button>
        </div>
      </nav>

      {/* Main Content */}
      <main className="flex-1 ml-14 max-md:ml-0 max-md:pb-16">
        <div className="max-w-[1440px] mx-auto p-8 max-md:p-4">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
