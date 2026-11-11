"""Real SDK against local HTTP fixtures: no model charges, no PostgreSQL writes."""
import asyncio
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from openai import AsyncOpenAI
from app.config import settings
from app.services.organize_model import RealOrganizeModel, ChunkInput, ModelError
from app.services.embedding_service import ClassroomEmbedding, get_classroom_embedding
from app.services.trial_budget import reserve_call, record_usage, reconcile_recorded_usage
from app.services.oss_service import OSSService


class Providers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        keys = ["OPENAI_API_KEY", "CLASSROOM_PAID_CALLS_ENABLED", "CLASSROOM_TRIAL_LEDGER",
                "CLASSROOM_TRIAL_BUDGET_CNY", "INDEX_EMBEDDING_PROVIDER"]
        self.old = {k: getattr(settings, k) for k in keys}
        settings.OPENAI_API_KEY = "fixture-key"
        settings.CLASSROOM_PAID_CALLS_ENABLED = True
        settings.CLASSROOM_TRIAL_LEDGER = str(Path(self.tmp.name) / "budget.sqlite3")
        settings.CLASSROOM_TRIAL_BUDGET_CNY = 10
        settings.INDEX_EMBEDDING_PROVIDER = "real"

    def tearDown(self):
        for k, value in self.old.items():
            setattr(settings, k, value)
        self.tmp.cleanup()

    def client(self, handler):
        return AsyncOpenAI(api_key="fixture-key", base_url="https://fixture.invalid/v1",
                           max_retries=0, http_client=httpx.AsyncClient(
                               transport=httpx.MockTransport(handler)))

    async def test_real_organize_sdk_contract(self):
        calls = []
        out = {"conclusions": [{"text": "力等于质量乘加速度", "quote": "力等于质量乘加速度",
                                "segment_index": 7, "origin": "teacher_explicit", "status": "confirmed"}],
               "tasks": []}
        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "fixture", "object": "chat.completion",
                "created": 1, "model": "qwen-plus", "choices": [{"index": 0,
                "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(out)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})
        with patch("openai.AsyncOpenAI", side_effect=lambda **kw: self.client(handler)):
            result = await RealOrganizeModel().extract_chunk(ChunkInput(0, [
                {"seq_index": 7, "content": "力等于质量乘加速度"}], []))
        self.assertEqual(result, out)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})
        self.assertFalse(calls[0]["enable_thinking"])
        self.assertLessEqual(calls[0]["max_tokens"], 600)

    async def test_malformed_json_is_terminal(self):
        def handler(request):
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 1,
                "model": "qwen-plus", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "not JSON"}}]})
        with patch("openai.AsyncOpenAI", side_effect=lambda **kw: self.client(handler)):
            with self.assertRaises(ModelError) as caught:
                await RealOrganizeModel().extract_chunk(ChunkInput(0, [], []))
        self.assertFalse(caught.exception.retryable)

    async def test_real_embedding_and_dimension_validation(self):
        calls = []
        dim = 1024
        def handler(request):
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(200, json={"object": "list", "model": "text-embedding-v3",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1] * dim}],
                "usage": {"prompt_tokens": 10, "total_tokens": 10}})
        with patch("app.services.embedding_service.AsyncOpenAI", side_effect=lambda **kw: self.client(handler)):
            self.assertIsInstance(get_classroom_embedding(), ClassroomEmbedding)
            rows = await ClassroomEmbedding().embed_batch(["牛顿第二定律"])
            self.assertEqual(len(rows[0]), 1024)
            self.assertEqual(calls[0]["dimensions"], 1024)
            dim = 3
            with self.assertRaises(ValueError):
                await ClassroomEmbedding().embed_batch(["牛顿第二定律"])

    async def test_disabled_trial_never_calls_network(self):
        settings.CLASSROOM_PAID_CALLS_ENABLED = False
        with patch("openai.AsyncOpenAI") as client:
            with self.assertRaises(ModelError):
                await RealOrganizeModel().extract_chunk(ChunkInput(0, [], []))
            client.assert_not_called()

    async def test_concurrent_budget_reservations_cap_at_ten(self):
        def reserve(_):
            try:
                reserve_call("embedding", "text-embedding-v3")
                return True
            except RuntimeError:
                return False
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(sum(pool.map(reserve, range(20))), 10)
        with self.assertRaises(RuntimeError):
            reserve_call("embedding", "text-embedding-v3")

    async def test_private_oss_signed_url_and_cleanup_key(self):
        service = OSSService.__new__(OSSService)
        service._enabled = True
        service._bucket = Mock()
        endpoint = settings.OSS_ENDPOINT or "https://oss-cn-beijing.aliyuncs.com"
        with patch.object(settings, "OSS_ENDPOINT", endpoint), patch.object(settings, "OSS_BUCKET_NAME", "fixture"):
            service._bucket.sign_url.return_value = "https://fixture." + endpoint.split("://")[-1] + "/audio/a.wav?signature=secret"
            url = service.upload_audio("fixture.wav")
            service._bucket.sign_url.assert_called_once()
            self.assertTrue(service.delete_object(url))
            service._bucket.delete_object.assert_called_once_with("audio/a.wav")

    async def test_known_usage_settles_but_unknown_keeps_reservation(self):
        import sqlite3
        from contextlib import closing
        known = reserve_call("organize", "qwen-plus")
        record_usage(known, {"total_tokens": 1500})
        unknown = reserve_call("embedding", "text-embedding-v3")
        record_usage(unknown, {})
        self.assertEqual(reconcile_recorded_usage(), 2)
        with closing(sqlite3.connect(settings.CLASSROOM_TRIAL_LEDGER)) as conn:
            rows = dict(conn.execute("SELECT id, reserved_cny FROM calls"))
        self.assertAlmostEqual(rows[known], 0.15)
        self.assertEqual(rows[unknown], 1)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
