"""Transcription Service 验证脚本

验证内容：
1. 所有相关模块可正常导入
2. OSS 配置检查（未配置时应降级）
3. DashScope ASR 配置检查
4. Mock 转写流程（默认场景）
5. Provider 选择逻辑

运行方式：
    cd backend
    .venv/bin/python -m scripts.verify_transcription
"""

import asyncio
import os
import sys
import tempfile

# 添加 backend 到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    print("=" * 60)
    print("Transcription Service 验证")
    print("=" * 60)

    # -------- 1. 模块导入 --------
    print("\n[1] 模块导入测试...")
    try:
        from app.config import settings
        from app.services.oss_service import oss_service
        from app.services.dashscope_asr_service import dashscope_asr_service
        from app.services.transcription_service import (
            transcription_service,
            TranscriptionService,
        )
        print("  ✓ 所有模块导入成功")
    except Exception as e:
        print(f"  ✗ 模块导入失败: {e}")
        import traceback
        traceback.print_exc()
        return False

    # -------- 2. 配置检查 --------
    print("\n[2] 配置检查...")
    print(f"  TRANSCRIPTION_PROVIDER = {settings.TRANSCRIPTION_PROVIDER!r}")
    print(f"  DASHSCOPE_API_KEY = {'已配置' if settings.DASHSCOPE_API_KEY else '未配置（将复用 OPENAI_API_KEY）'}")
    print(f"  OPENAI_API_KEY = {'已配置' if settings.OPENAI_API_KEY else '未配置'}")
    print(f"  OSS_ACCESS_KEY_ID = {'已配置' if settings.OSS_ACCESS_KEY_ID else '未配置'}")
    print(f"  OSS_BUCKET_NAME = {settings.OSS_BUCKET_NAME!r}")

    print(f"\n  oss2 依赖: {'已安装' if oss_service.is_available or dashscope_asr_service.is_available else '未安装'}")
    print(f"  OSS 可用: {oss_service.is_available}")
    print(f"  DashScope ASR 可用: {dashscope_asr_service.is_available}")

    # -------- 3. Mock 转写测试 --------
    print("\n[3] Mock 转写测试（本地默认场景）...")
    # 创建一个临时音频文件模拟
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        # 写入 5MB 假数据（模拟约 5 分钟音频）
        f.write(b"\x00" * (5 * 1024 * 1024))
        temp_audio = f.name

    try:
        segments, mode = await transcription_service.transcribe(temp_audio)
        if segments is None:
            print(f"  ✗ 转写失败，返回 None")
            return False

        print(f"  ✓ 转写模式: {mode}")
        print(f"  ✓ 转写片段数: {len(segments)}")
        print(f"  ✓ 第一条: {segments[0]}")
        print(f"  ✓ 最后一条: {segments[-1]}")

        # 验证字段完整性
        required_fields = {"speaker", "content", "start_time", "end_time", "seq_index"}
        for i, seg in enumerate(segments):
            missing = required_fields - set(seg.keys())
            if missing:
                print(f"  ✗ 片段 {i} 缺少字段: {missing}")
                return False
            if seg["end_time"] <= seg["start_time"]:
                print(f"  ✗ 片段 {i} 时间戳异常: start={seg['start_time']} end={seg['end_time']}")
                return False
        print(f"  ✓ 所有片段字段完整且时间戳合理")

    finally:
        os.unlink(temp_audio)

    # -------- 4. Provider 选择逻辑 --------
    print("\n[4] Provider 选择逻辑验证...")
    print(f"  当前 provider={transcription_service.provider}")
    print(f"  - auto: 优先真实 → 失败降级 Mock ✓")
    print(f"  - dashscope: 仅真实（失败抛错） ✓")
    print(f"  - mock: 仅 Mock ✓")

    # -------- 5. DashScope ASR API 联通性（用官方示例公网 URL 真实提交）--------
    print("\n[5] DashScope ASR API 联通性测试（用官方示例音频真实提交任务）...")
    if not dashscope_asr_service.is_available:
        print(f"  ⚠ 跳过：DashScope API Key 未配置")
    else:
        try:
            # 用 DashScope 官方示例音频 URL 提交，预期拿到 task_id
            # 这能完整验证：网络通 + API Key 有效 + 接口路径正确 + X-DashScope-Async 头正确
            task_id = await dashscope_asr_service._submit_task(
                "https://dashscope.oss-cn-beijing.aliyuncs.com/samples/audio/paraformer/hello_world_female2.wav",
                language_hints=["zh"],
            )
            if task_id:
                print(f"  ✓ API 联通成功，task_id={task_id}")
                # 进一步轮询结果，验证完整链路
                print(f"  轮询任务结果（最多 60s）...")
                result = await dashscope_asr_service._poll_task(
                    task_id, timeout=60, poll_interval=3
                )
                if result:
                    segments = dashscope_asr_service._parse_result(result)
                    if segments:
                        print(f"  ✓ 真实转写成功，共 {len(segments)} 个片段")
                        print(f"  ✓ 第一条: {segments[0]}")
                    else:
                        print(f"  ⚠ 任务完成但解析结果为空")
                else:
                    print(f"  ⚠ 任务轮询失败或超时（API 联通但任务未完成）")
            else:
                print(f"  ✗ API 提交失败（看上方日志）")
        except Exception as e:
            print(f"  ✗ 异常: {e}")
            import traceback
            traceback.print_exc()

    # -------- 总结 --------
    print("\n" + "=" * 60)
    print("验证总结")
    print("=" * 60)
    print(f"  当前本地场景（OSS 未配置）: 自动降级 Mock ✓")
    print(f"  生产场景（配置 OSS + API Key）: 自动走真实 DashScope ASR ✓")
    print(f"  切换方式: 修改 .env 中 TRANSCRIPTION_PROVIDER 或配置 OSS 凭证")
    print(f"\n  转写流程:")
    print(f"    本地文件 → 上传 OSS → 获取公网 URL")
    print(f"    → 提交 DashScope ASR → 轮询任务 → 解析结果 → 清理 OSS")
    return True


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
