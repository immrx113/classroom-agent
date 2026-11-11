// Socket.IO 信令通道
//
// 信令事件（客户端 → 服务端）：
//   join          { roomId, displayName }     加入房间
//   getRouterRtp  {}                          获取 router RTP capabilities
//   createTransport { direction }             创建 WebRtcTransport
//   connectTransport { transportId, dtlsParams }
//   produce       { transportId, kind, rtpParams, appData }
//   consume       { producerId, rtpCapabilities }
//   leave         {}                          离开房间
//
// 信令事件（服务端 → 客户端）：
//   joined        { peerId, peers, routerRtpCapabilities }
//   peer-joined   { peerId, displayName }
//   peer-left     { peerId }
//   new-producer  { producerId, peerId, displayName, kind, appData }
//   active-speaker { speakers }
//   transport-created { transportId, iceParameters, iceCandidates, dtlsParameters }
//   consumed      { id, producerId, kind, rtpParameters }

import { Server as IOServer } from 'socket.io'
import { Room } from './room.js'

export class Signaling {
  constructor(httpServer, worker, roomRegistry) {
    this.worker = worker
    this.rooms = roomRegistry     // Map<roomId, Room>
    const SIGNALING_CORS = (process.env.CORS_ORIGIN || 'http://localhost:5173').split(',')
    this.io = new IOServer(httpServer, {
      cors: { origin: SIGNALING_CORS, methods: ['GET', 'POST'] },
    })
  }

  start() {
    this.io.on('connection', (socket) => {
      console.log(`[Signaling] socket connected ${socket.id}`)
      let currentRoom = null
      let currentPeerId = null

      // ── join ──
      socket.on('join', async ({ roomId, displayName }, ack) => {
        try {
          let room = this.rooms.get(roomId)
          if (!room) {
            // 房间不存在，自动创建（也可由 REST API 预创建）
            room = new Room(roomId, this.worker)
            await room.init()
            room.attachIO(this.io)
            this.rooms.set(roomId, room)
            console.log(`[Signaling] 自动创建房间 ${roomId}`)
          }

          const peer = room.addPeer(socket.id, (displayName || '').replace(/<[^>]*>/g, '').slice(0, 50))
          currentRoom = room
          currentPeerId = peer.id
          socket.join(roomId)  // socket.io 房间

          // 通知房间其他人
          socket.to(roomId).emit('peer-joined', {
            peerId: peer.id,
            displayName: peer.displayName,
          })

          // 返回当前房间状态
          const peers = Array.from(room.peers.values())
            .filter((p) => p.id !== peer.id)
            .map((p) => ({
              peerId: p.id,
              displayName: p.displayName,
              producers: Array.from(p.producers.values()).map((prod) => ({
                producerId: prod.id,
                kind: prod.kind,
                appData: prod.appData,
              })),
            }))

          ack?.({
            ok: true,
            peerId: peer.id,
            peers,
            routerRtpCapabilities: room.getRtpCapabilities(),
          })
        } catch (e) {
          console.error('[Signaling] join failed', e)
          ack?.({ ok: false, error: e.message })
        }
      })

      // ── getRouterRtp ──
      socket.on('getRouterRtp', (_, ack) => {
        if (!currentRoom) return ack?.({ ok: false, error: 'not joined' })
        ack?.({ ok: true, rtpCapabilities: currentRoom.getRtpCapabilities() })
      })

      // ── createTransport ──
      socket.on('createTransport', async ({ direction }, ack) => {
        try {
          const transport = await currentRoom.createWebRtcTransport(
            currentPeerId, direction,
          )
          ack?.({
            ok: true,
            transportId: transport.id,
            iceParameters: transport.iceParameters,
            iceCandidates: transport.iceCandidates,
            dtlsParameters: transport.dtlsParameters,
          })
        } catch (e) {
          ack?.({ ok: false, error: e.message })
        }
      })

      // ── connectTransport ──
      socket.on('connectTransport', async ({ transportId, dtlsParameters }, ack) => {
        try {
          await currentRoom.connectTransport(currentPeerId, transportId, dtlsParameters)
          ack?.({ ok: true })
        } catch (e) {
          ack?.({ ok: false, error: e.message })
        }
      })

      // ── produce ──
      socket.on('produce', async ({ transportId, kind, rtpParameters, appData }, ack) => {
        try {
          const producer = await currentRoom.produce(
            currentPeerId, transportId, kind, rtpParameters, appData,
          )
          ack?.({ ok: true, producerId: producer.id })
        } catch (e) {
          ack?.({ ok: false, error: e.message })
        }
      })

      // ── consume ──
      socket.on('consume', async ({ producerId, rtpCapabilities }, ack) => {
        try {
          const result = await currentRoom.consume(
            currentPeerId, producerId, rtpCapabilities,
          )
          ack?.({ ok: true, ...result })
        } catch (e) {
          ack?.({ ok: false, error: e.message })
        }
      })

      // ── leave / disconnect ──
      const handleLeave = () => {
        if (!currentRoom || !currentPeerId) return
        socket.to(currentRoom.roomId).emit('peer-left', { peerId: currentPeerId })
        currentRoom.removePeer(currentPeerId)
        // 房间空了 → 销毁
        if (currentRoom.peers.size === 0) {
          console.log(`[Signaling] 房间空了，销毁 ${currentRoom.roomId}`)
          currentRoom.close()
          this.rooms.delete(currentRoom.roomId)
        }
        currentRoom = null
        currentPeerId = null
      }
      socket.on('leave', handleLeave)
      socket.on('disconnect', () => {
        console.log(`[Signaling] socket disconnected ${socket.id}`)
        handleLeave()
      })
    })
  }
}
