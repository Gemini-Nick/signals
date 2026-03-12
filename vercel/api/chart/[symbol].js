import { Redis } from '@upstash/redis'

const redis = Redis.fromEnv()

export default async function handler(req, res) {
  const { symbol } = req.query
  const freq = req.query.freq || 'daily'

  if (!['daily', '30min', '15min'].includes(freq)) {
    return res.status(400).json({ detail: 'freq 必须是 daily/30min/15min' })
  }

  const key = `signals:chart:${decodeURIComponent(symbol)}:${freq}`
  const data = await redis.get(key)

  if (!data) {
    return res.status(404).json({ detail: `未找到: ${decodeURIComponent(symbol)} ${freq}` })
  }

  res.setHeader('Cache-Control', 's-maxage=60, stale-while-revalidate=300')
  return res.status(200).json(data)
}
