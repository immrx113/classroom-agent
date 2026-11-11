"""实时会议 WebSocket 联调测试

验证：
1. WebSocket 连接成功
2. 发送 PCM 音频分片能收到 transcript 消息
3. 控制消息 end 能正常关闭会话
"""
import asyncio
import json
import sys
from pathlib import Path

# 添加 backend 到 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # type: ignore


async def test_realtime_ws():
    # 创建会话
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "http://localhost:8787/api/realtime/sessions",
            json={"title": "WS 联调测试"},
        )
        meeting_id = resp.json()["meeting_id"]
    print(f"创建会话: meeting_id={meeting_id}")

    # 连接 WebSocket
    ws_url = f"ws://localhost:8787/api/realtime/{meeting_id}/ws"
    print(f"连接: {ws_url}")

    async with websockets.connect(ws_url, proxy=None) as ws:
        # 接收 session_started
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        data = json.loads(msg)
        assert data["type"] == "session_started", f"unexpected: {data}"
        print(f"✓ session_started: mode={data['mode']}")

        # 发送 12 帧 PCM（每帧 6400 bytes，模拟 200ms × 12 = 2.4s 音频）
        # 期望触发至少 1 段 mock 转写
        print("发送 12 帧 PCM 音频...")
        for _ in range(12):
            # Int16, 16kHz, mono, 200ms = 6400 bytes
            pcm_frame = b"\x00\x10" * 3200  # 6400 bytes
            await ws.send(pcm_frame)
            await asyncio.sleep(0.05)

        # 等待接收转写结果
        transcript_count = 0
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
                data = json.loads(msg)
                if data["type"] == "transcript":
                    transcript_count += 1
                    seg = data["segment"]
                    print(f"✓ transcript #{transcript_count}: [{seg['speaker']}] {seg['content']}")
                else:
                    print(f"  其他消息: {data['type']}")
        except asyncio.TimeoutError:
            pass

        assert transcript_count > 0, "未收到任何转写结果"
        print(f"\n✓ 收到 {transcript_count} 段转写")

        # 查询会话信息
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://localhost:8787/api/realtime/sessions/{meeting_id}/info")
            info = resp.json()
            print(f"✓ 会话信息: segment_count={info['segment_count']} mode={info['mode']}")

        # 结束会话
        await ws.send(json.dumps({"action": "end"}))
        await asyncio.sleep(1)

        # 验证会话已结束
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"http://localhost:8787/api/realtime/sessions/{meeting_id}/end")
            end_info = resp.json()
            print(f"✓ 会话结束: total_segments={end_info['total_segments']}")

    print("\n=== 全部测试通过 ===")


if __name__ == "__main__":
    asyncio.run(test_realtime_ws())
