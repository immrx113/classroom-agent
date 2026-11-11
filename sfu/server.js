// mediasoup SFU 服务入口
//
// 启动：
//   node server.js
//
// 端口：
//   - HTTP/WS 信令：4001（SFU_PORT）
//   - RTC 媒体端口：40000-49999（MEDIASOUP_MIN_PORT ~ MAX_PORT）

import 'dotenv/config'
import express from 'express'
import cors from 'cors'
import http from 'http'
import crypto from 'crypto'
import mediasoup from 'mediasoup'

import { config } from './config.js'
import { Signaling } from './lib/signaling.js'

// ── mediasoup Worker（单进程多 Router） ──
let worker = null
const rooms = new Map()  // roomId → Room

async function startWorker() {
  worker = await mediasoup.createWorker({
    logLevel: config.worker.logLevel,
    logTags: config.worker.logTags,
    rtcMinPort: config.worker.rtcMinPort,
    rtcMaxPort: config.worker.rtcMaxPort,
  })
  worker.on('died', () => {
    console.error('[SFU] mediasoup worker died, exiting in 2s...')
    setTimeout(() => process.exit(1), 2000)
  })
  console.log(`[SFU] worker started, rtcPorts=${config.worker.rtcMinPort}-${config.worker.rtcMaxPort}`)
}

// ── Express HTTP API（房间管理） ──
const app = express()
const CORS_ORIGIN = process.env.CORS_ORIGIN || 'http://localhost:5173'
app.use(cors({ origin: CORS_ORIGIN.split(',') }))
app.use(express.json())

// API Key 认证中间件
const SFU_API_KEY = process.env.SFU_API_KEY || ''
function apiKeyAuth(req, res, next) {
  // health 端点免认证
  if (req.path === '/health') return next()
  // 未配置 API Key 时放行（开发模式）
  if (!SFU_API_KEY) return next()
  const key = req.headers['x-api-key'] || req.query.api_key
  if (key !== SFU_API_KEY) {
    return res.status(401).json({ error: 'unauthorized' })
  }
  next()
}
app.use(apiKeyAuth)

// 健康检查
app.get('/health', (req, res) => {
  res.json({
    status: 'ok',
    worker: worker ? 'running' : 'starting',
    rooms: rooms.size,
    uptime: process.uptime(),
  })
})

// 创建房间
app.post('/rooms', (req, res) => {
  const { title, scene } = req.body || {}
  const roomId = crypto.randomUUID()
  // 注意：Room 实例在第一个客户端 join 时才创建（lazy init）
  // 这里只生成 ID，避免空房间占用资源
  res.json({
    roomId,
    title: title || `会议 ${roomId.slice(0, 8)}`,
    scene: scene || 'generic',
    sfuRouterId: null,  // join 时由 signaling 填充
  })
})

// 查询房间
app.get('/rooms/:roomId', (req, res) => {
  const room = rooms.get(req.params.roomId)
  if (!room) {
    return res.status(404).json({ error: 'room not found or empty' })
  }
  res.json({
    roomId: room.roomId,
    peerCount: room.peers.size,
    activeSpeakers: room.activeSpeakers,
    createdAt: room.createdAt,
  })
})

// 结束房间
app.post('/rooms/:roomId/end', (req, res) => {
  const room = rooms.get(req.params.roomId)
  if (room) {
    room.close()
    rooms.delete(req.params.roomId)
  }
  res.json({ ok: true })
})

// ── 启动 ──
const httpServer = http.createServer(app)
const signaling = new Signaling(httpServer, null, rooms)  // worker 后注入

async function main() {
  await startWorker()
  signaling.worker = worker
  signaling.start()

  httpServer.listen(config.serverPort, () => {
    console.log(`[SFU] signaling listening on :${config.serverPort}`)
    console.log(`[SFU] HTTP API: http://localhost:${config.serverPort}`)
  })
}

main().catch((e) => {
  console.error('[SFU] 启动失败', e)
  process.exit(1)
})
