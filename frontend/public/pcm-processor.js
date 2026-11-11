/**
 * AudioWorklet PCM 处理器
 *
 * 职责：
 * 1. 接收 AudioWorkletNode 输入的 Float32 音频帧
 * 2. 累积到 200ms 缓冲（16kHz × 0.2s = 3200 samples）
 * 3. 转 Int16 PCM 格式
 * 4. 通过 port.postMessage 传给主线程（transferable，零拷贝）
 *
 * 注：AudioWorklet 在独立线程执行，避免阻塞主线程 UI
 */
class PCMProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    // 16kHz × 0.2s = 3200 samples（每帧 200ms）
    this.bufferSize = 3200
    this.buffer = new Float32Array(this.bufferSize)
    this.offset = 0
  }

  process(inputs) {
    const input = inputs[0][0]
    if (!input) return true

    for (let i = 0; i < input.length; i++) {
      this.buffer[this.offset++] = input[i]

      if (this.offset >= this.bufferSize) {
        // Float32 [-1, 1] → Int16 [-32768, 32767]
        const pcm16 = new Int16Array(this.bufferSize)
        for (let j = 0; j < this.bufferSize; j++) {
          const s = Math.max(-1, Math.min(1, this.buffer[j]))
          pcm16[j] = s < 0 ? s * 0x8000 : s * 0x7fff
        }
        // transferable 零拷贝传递
        this.port.postMessage(pcm16.buffer, [pcm16.buffer])
        this.offset = 0
      }
    }
    return true
  }
}

registerProcessor('pcm-processor', PCMProcessor)
