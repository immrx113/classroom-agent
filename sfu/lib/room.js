// 房间管理：每房间一个 mediasoup Router + AudioLevelObserver
//
// 职责：
//   1. 创建/销毁 Router
//   2. 管理参与者（peer）的 transports / producers / consumers
//   3. 新 Producer 出现时自动广播给其他 peer（订阅模型）
//   4. AudioLevelObserver 推送活跃说话人

import { config } from '../config.js'

let _nextPeerId = 1

export class Room {
  constructor(roomId, worker) {
    this.roomId = roomId
    this.worker = worker
    this.peers = new Map()      // peerId → Peer
    this.router = null
    this.audioLevelObserver = null
    this.activeSpeakers = []    // [{ peerId, producerId, volume }]
    this.createdAt = Date.now()
    this.closed = false
  }

  async init() {
    this.router = await this.worker.createRouter({
      mediaCodecs: config.routerMediaCodecs,
    })

    this.audioLevelObserver = await this.router.createAudioLevelObserver({
      maxEntries: config.audioLevelObserver.maxEntries,
      threshold: config.audioLevelObserver.threshold,
      interval: config.audioLevelObserver.interval,
    })

    // 活跃说话人变化时，广播给房间所有 peer
    this.audioLevelObserver.on('volumes', (volumes) => {
      this.activeSpeakers = volumes.map((v) => ({
        peerId: v.producer.appData.peerId,
        producerId: v.producer.id,
        volume: v.volume,
      }))
      this._broadcast('active-speaker', { speakers: this.activeSpeakers })
    })

    this.audioLevelObserver.on('silence', () => {
      this.activeSpeakers = []
      this._broadcast('active-speaker', { speakers: [] })
    })
  }

  addPeer(socketId, displayName) {
    const MAX_PEERS = 30
    if (this.peers.size >= MAX_PEERS) {
      throw new Error(`房间已满，最多允许 ${MAX_PEERS} 人`)
    }
    const peerId = String(_nextPeerId++)
    const peer = {
      id: peerId,
      socketId,
      displayName: displayName || `用户${peerId}`,
      transports: new Map(),   // transportId → WebRtcTransport
      producers: new Map(),    // producerId → Producer
      consumers: new Map(),    // consumerId → Consumer
    }
    this.peers.set(peerId, peer)
    return peer
  }

  getPeer(peerId) {
    return this.peers.get(peerId)
  }

  getPeerBySocket(socketId) {
    for (const peer of this.peers.values()) {
      if (peer.socketId === socketId) return peer
    }
    return null
  }

  removePeer(peerId) {
    const peer = this.peers.get(peerId)
    if (!peer) return

    for (const transport of peer.transports.values()) {
      transport.close()
    }
    for (const producer of peer.producers.values()) {
      producer.close()
    }
    for (const consumer of peer.consumers.values()) {
      consumer.close()
    }
    this.peers.delete(peerId)
  }

  // 创建 WebRtcTransport（用于 send / recv）
  async createWebRtcTransport(peerId, direction) {
    const peer = this.getPeer(peerId)
    if (!peer) throw new Error(`peer ${peerId} not found`)

    const transport = await this.router.createWebRtcTransport({
      listenIps: config.webRtcTransport.listenIps,
      enableUdp: config.webRtcTransport.enableUdp,
      enableTcp: config.webRtcTransport.enableTcp,
      preferUdp: config.webRtcTransport.preferUdp,
      initialAvailableOutgoingBitrate:
        config.webRtcTransport.initialAvailableOutgoingBitrate,
      appData: { direction, peerId },
    })

    if (config.webRtcTransport.maxIncomingBitrate) {
      try {
        await transport.setMaxIncomingBitrate(
          config.webRtcTransport.maxIncomingBitrate,
        )
      } catch (e) {
        console.warn('[Room] setMaxIncomingBitrate failed', e.message)
      }
    }

    transport.on('dtlsstatechange', (state) => {
      if (state === 'closed') transport.close()
    })

    peer.transports.set(transport.id, transport)
    return transport
  }

  // 客户端 connect transport（DTLS 握手完成）
  async connectTransport(peerId, transportId, dtlsParameters) {
    const peer = this.getPeer(peerId)
    if (!peer) throw new Error(`peer ${peerId} not found`)
    const transport = peer.transports.get(transportId)
    if (!transport) throw new Error(`transport ${transportId} not found`)
    await transport.connect({ dtlsParameters })
  }

  // 客户端 produce：发布本地音视频流
  async produce(peerId, transportId, kind, rtpParameters, appData = {}) {
    const peer = this.getPeer(peerId)
    if (!peer) throw new Error(`peer ${peerId} not found`)
    const transport = peer.transports.get(transportId)
    if (!transport) throw new Error(`transport ${transportId} not found`)

    const producer = await transport.produce({
      kind,
      rtpParameters,
      appData: { ...appData, peerId, displayName: peer.displayName },
    })

    peer.producers.set(producer.id, producer)

    // 音频流加入 AudioLevelObserver
    if (kind === 'audio') {
      this.audioLevelObserver.addProducer({ producerId: producer.id })
    }

    producer.on('transportclose', () => {
      peer.producers.delete(producer.id)
    })

    // 通知其他 peer：新 producer 出现，可以订阅
    this._broadcast(
      'new-producer',
      {
        producerId: producer.id,
        peerId,
        displayName: peer.displayName,
        kind,
        appData: producer.appData,
      },
      peerId,  // 排除自己
    )

    return producer
  }

  // 客户端 consume：订阅他人 producer
  async consume(peerId, producerId, rtpCapabilities) {
    const peer = this.getPeer(peerId)
    if (!peer) throw new Error(`peer ${peerId} not found`)

    // 找到 producer 所属 peer
    let producer = null
    for (const p of this.peers.values()) {
      if (p.producers.has(producerId)) {
        producer = p.producers.get(producerId)
        break
      }
    }
    if (!producer) throw new Error(`producer ${producerId} not found`)

    // 检查 router 是否能消费
    if (!this.router.canConsume({ producerId, rtpCapabilities })) {
      throw new Error('router cannot consume')
    }

    // 找到该 peer 的 recv transport（按 appData.direction 找）
    let transport = null
    for (const t of peer.transports.values()) {
      if (t.appData.direction === 'recv') {
        transport = t
        break
      }
    }
    if (!transport) throw new Error('peer has no recv transport')

    const consumer = await transport.consume({
      producerId,
      rtpParameters: producer.rtpParameters,
      appData: { peerId, producerId },
      paused: false,
    })

    peer.consumers.set(consumer.id, consumer)

    consumer.on('transportclose', () => {
      peer.consumers.delete(consumer.id)
    })
    consumer.on('producerclose', () => {
      peer.consumers.delete(consumer.id)
    })

    return {
      id: consumer.id,
      producerId,
      kind: consumer.kind,
      rtpParameters: consumer.rtpParameters,
    }
  }

  // 客户端获取 router RTP capabilities
  getRtpCapabilities() {
    return this.router.rtpCapabilities
  }

  // 关闭房间
  close() {
    if (this.closed) return
    this.closed = true
    for (const peer of this.peers.values()) {
      this.removePeer(peer.id)
    }
    this.router?.close()
    this.audioLevelObserver?.close()
  }

  // ── 内部：广播给房间所有 peer（通过 socket.io） ──
  _broadcast(event, data, excludePeerId = null) {
    // 通过 io 实例广播，由 signaling.js 注入
    if (this._io) {
      for (const peer of this.peers.values()) {
        if (peer.id === excludePeerId) continue
        this._io.to(peer.socketId).emit(event, data)
      }
    }
  }

  attachIO(io) {
    this._io = io
  }
}
