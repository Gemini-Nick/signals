import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from '@/components/Layout'
import Toast from '@/components/Toast'
import ClusterPage from '@/pages/ClusterPage'
import ChartPage from '@/pages/ChartPage'
import BacktestPage from '@/pages/BacktestPage'

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<ClusterPage />} />
          <Route path="chart" element={<ChartPage />} />
          <Route path="backtest" element={<BacktestPage />} />
        </Route>
      </Routes>
      <Toast />
    </BrowserRouter>
  )
}
