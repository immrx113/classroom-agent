// mediasoup 配置
// 文档：https://mediasoup.org/documentation/v3/mediasoup/api/

const _announcedIp = process.env.ANNOUNCED_IP || '127.0.0.1'
if (_announcedIp === '127.0.0.1') {
  console.warn('[Config] announcedIp is 127.0.0.1 — this will NOT work in production. Set ANNOUNCED_IP to your public IP.')
}

export const config = {
  // mediasoup Worker 进程配置
  worker: {
    rtcMinPort: Number(process.env.MEDIASOUP_MIN_PORT) || 40000,
    rtcMaxPort: Number(process.env.MEDIASOUP_MAX_PORT) || 49999,
    logLevel: 'warn',
    logTags: [
      'info',
      'ice',
      'dtls',
      'rtp',
      'srtp',
      'rtcp',
    ],
  },

  // Router（每房间一个）的 RTP capabilities
  // 音频编解码：opus（必备）+ 视频：VP8 / H264
  routerMediaCodecs: [
    {
      kind: 'audio',
      mimeType: 'audio/opus',
      clockRate: 48000,
      channels: 2,
    },
    {
      kind: 'video',
      mimeType: 'video/VP8',
      clockRate: 90000,
    },
    {
      kind: 'video',
      mimeType: 'video/h264',
      clockRate: 90000,
      parameters: {
        'packetization-mode': 1,
        'profile-level-id': '4d0032',
        'level-asymmetry-allowed': 1,
        'x-google-start-bitrate': 1000,
      },
    },
  ],

  // WebRtcTransport 配置
  webRtcTransport: {
    listenIps: [
      {
        ip: process.env.LISTEN_IP || '0.0.0.0',
        // 公网/局域网 IP，开发环境用 127.0.0.1
        announcedIp: _announcedIp,
      },
    ],
    initialAvailableOutgoingBitrate: 1_000_000,
    maxIncomingBitrate: 1_500_000,
    enableUdp: true,
    enableTcp: true,
    preferUdp: true,
  },

  // AudioLevelObserver：识别活跃说话人
  audioLevelObserver: {
    maxEntries: 2,        // Top-2 活跃说话人
    threshold: -70,       // dB 阈值
    interval: 500,        // 500ms 推送一次
  },

  // 服务端口
  serverPort: Number(process.env.SFU_PORT) || 4001,
}
