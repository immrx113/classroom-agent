-- 原子令牌桶（专项「Checkpoint + AI 调用控制」S2）
-- 一次 Lua 完成：Redis TIME 取时、读状态、补充令牌、检查/扣减、写回、PEXPIRE。
-- 应用不使用「先 GET 再 DECR」，避免非原子读改写导致并发超发。
--
-- KEYS[1] = 桶 key（Hash 存 tokens / last_ms）
-- ARGV[1] = capacity（令牌容量，>0）
-- ARGV[2] = refill_per_second（每秒补充速率，>0）
-- ARGV[3] = cost（单次请求消耗，0 < cost <= capacity）
-- 返回：[allowed(0|1), remaining_tokens, retry_after_ms]
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

-- 非法配置明确拒绝（应用侧调用前也做同源校验，此处为纵深防御）。
if capacity == nil or capacity <= 0 then
  return redis.error_reply('ERR invalid capacity')
end
if refill == nil or refill <= 0 then
  return redis.error_reply('ERR invalid refill')
end
if cost == nil or cost <= 0 or cost > capacity then
  return redis.error_reply('ERR invalid cost')
end

-- Redis TIME 取时：[seconds, microseconds]，与客户端时钟无关（CA-12 真实并发边界）。
local t = redis.call('TIME')
local now_ms = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

local tokens = tonumber(redis.call('HGET', key, 'tokens'))
local last_ms = tonumber(redis.call('HGET', key, 'last_ms'))
if tokens == nil or last_ms == nil then
  -- 桶不存在：初始为满桶，last_ms 记为当前时刻（空闲过期重建满桶不额外超发）。
  tokens = capacity
  last_ms = now_ms
end

-- 补充：时钟回退时 elapsed 归零，不倒退 last_ms、不多补令牌。
local elapsed = now_ms - last_ms
if elapsed < 0 then elapsed = 0 end
local available = tokens + (elapsed * refill / 1000)
if available > capacity then available = capacity end

local allowed = 0
local remaining = available
local retry_after_ms = 0
if available >= cost then
  allowed = 1
  remaining = available - cost
else
  retry_after_ms = math.ceil((cost - available) / refill * 1000)
end

-- last_ms = max(now_ms, last_ms)：时钟回退不倒退。
if now_ms > last_ms then last_ms = now_ms end

redis.call('HSET', key, 'tokens', remaining, 'last_ms', last_ms)

-- 桶 TTL：max(60000, ceil(capacity / refill * 2000)) ms。
local ttl_ms = math.ceil(capacity / refill * 2000)
if ttl_ms < 60000 then ttl_ms = 60000 end
redis.call('PEXPIRE', key, ttl_ms)

-- 注意：Redis RESP2 会把 Lua 返回的 number 截断为整数（小数被丢弃），
-- 故 remaining 显式转字符串返回，避免应用侧拿到被截断的剩余令牌数。
return {allowed, tostring(remaining), retry_after_ms}
