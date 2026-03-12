import { Redis } from '@upstash/redis'

const redis = Redis.fromEnv()

export default async function handler(req, res) {
  const [status, meta] = await Promise.all([
    redis.get('signals:status'),
    redis.get('signals:meta'),
  ])

  const result = {
    ready: !!status,
    running: false,
    last_update: meta?.last_push_at || 0,
    error: '',
    index_count: meta?.index_count || 0,
    signal_count: 0,
  }

  if (status) {
    Object.assign(result, status)
  }

  res.setHeader('Cache-Control', 's-maxage=30, stale-while-revalidate=120')
  return res.status(200).json(result)
}
