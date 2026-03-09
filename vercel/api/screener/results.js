import { Redis } from '@upstash/redis'

const redis = Redis.fromEnv()

export default async function handler(req, res) {
  const data = await redis.get('signals:screener')
  res.setHeader('Cache-Control', 's-maxage=60, stale-while-revalidate=300')
  return res.status(200).json(data || [])
}
