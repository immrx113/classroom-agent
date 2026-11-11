"""应用配置管理"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置，从环境变量读取"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 应用
    APP_NAME: str = "Yuan-Meet · 研发会议垂类 Agent"
    DEBUG: bool = True
    SERVER_PORT: int = 8787

    # 身份与范围：个人版固定本地主体，未来登录身份复用同一字段
    LOCAL_SUBJECT_ID: str = "local-user"

    # SFU（mediasoup）服务地址，由 Step 1.3 启用
    SFU_BASE_URL: str = "http://localhost:4001"

    # 数据库
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/yuan_meet"
    DATABASE_ECHO: bool = False

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # LLM（通义千问，兼容 OpenAI 接口）
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL: str = "qwen-plus"
    VISION_MODEL: str = "qwen-vl-plus"
    EMBEDDING_MODEL: str = "text-embedding-v3"
    EMBEDDING_DIMENSIONS: int = 1024  # text-embedding-v3 维度

    # 音频转写：paraformer-v2 中文识别更优，whisper-1 作为兼容备选
    TRANSCRIPTION_MODEL: str = "paraformer-v2"
    # 转写 Provider：auto(默认,优先真实→降级Mock) / dashscope / mock
    TRANSCRIPTION_PROVIDER: str = "auto"

    # DashScope 原生录音识别 API（生产环境真实转写）
    # 文档：https://help.aliyun.com/zh/dashscope/developer-reference/paraformer-audio-file-recognition
    DASHSCOPE_API_KEY: str = ""  # 默认复用 OPENAI_API_KEY，留空则自动取
    DASHSCOPE_ASR_URL: str = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
    DASHSCOPE_TASK_QUERY_URL: str = "https://dashscope.aliyuncs.com/api/v1/tasks"
    DASHSCOPE_ASR_MODEL: str = "paraformer-v2"

    # 阿里云 OSS（DashScope ASR 需要公网 URL，通过 OSS 中转）
    # 留空则禁用真实转写，自动降级 Mock
    OSS_ACCESS_KEY_ID: str = ""
    OSS_ACCESS_KEY_SECRET: str = ""
    OSS_ENDPOINT: str = ""  # 如 https://oss-cn-hangzhou.aliyuncs.com
    OSS_BUCKET_NAME: str = ""
    OSS_PREFIX: str = "audio/"  # 上传到 OSS 的目录前缀

    # 文件上传
    UPLOAD_DIR: str = "./uploads"
    MAX_UPLOAD_SIZE: int = 500 * 1024 * 1024  # 500MB

    # 第二阶段「可靠输入」交付配置（冻结验收证据包绑定数值）
    # S：上传大小上限；C：读取块大小。均不随请求改变。
    MEDIA_UPLOAD_MAX_BYTES: int = 8 * 1024 * 1024  # S = 8MB
    MEDIA_UPLOAD_CHUNK_BYTES: int = 1024 * 1024  # C = 1MB
    # 声明支持的媒体容器/编码及供应商输入时长范围（毫秒）
    MEDIA_MIN_DURATION_MS: int = 500  # 0.5s
    MEDIA_MAX_DURATION_MS: int = 4 * 3600 * 1000  # 4h
    # 任务可靠性：T=worker 失联阈值（秒），R=自动重试总上限
    JOB_HEARTBEAT_TIMEOUT_SECONDS: int = 60  # T
    JOB_MAX_ATTEMPTS: int = 3  # R
    # 显式重试等待基准（秒），实际按 attempt 指数退避
    JOB_RETRY_BASE_SECONDS: int = 5

    # ASR 处理模式：real（真实 provider，缺配置即失败，不降级）/ fake（可控替身）/ demo（显式演示）
    ASR_MODE: str = "real"

    # 第三阶段「全课整理」交付配置（冻结验收证据包绑定数值）
    # 上下文预算按字符估算（保守口径 1 token ≈ 1 字符）：总预算 = 输入 + 提示 + 输出预留。
    ORGANIZE_CONTEXT_BUDGET: int = 4000       # 单块总上下文预算（字符）
    ORGANIZE_PROMPT_RESERVE: int = 400        # 提示词预留
    ORGANIZE_OUTPUT_RESERVE: int = 600        # 输出预留
    ORGANIZE_CHUNK_OVERLAP: int = 1           # 相邻块重叠片段数（消歧上下文）
    ORGANIZE_MAX_ATTEMPTS: int = 3            # 分块失败重试上限（R）
    ORGANIZE_MODEL: str = "qwen-plus"         # 整理模型（真实语义质量交 P1，自动验收用替身）
    # 本机首轮联调：显式开启才允许新课堂链路发起收费请求。
    CLASSROOM_PAID_CALLS_ENABLED: bool = False
    # 回退只读（CA-25）：置真时课堂新任务入口停写（503），只读原有 active 结果；
    # 不降级为旧无控制付费路径、不删除新增表/调用记录/账本。
    CLASSROOM_READ_ONLY: bool = False
    CLASSROOM_TRIAL_BUDGET_CNY: int = 10
    CLASSROOM_TRIAL_LEDGER: str = "./runtime/classroom-trial.sqlite3"
    INDEX_EMBEDDING_PROVIDER: str = "real"  # real / deterministic（仅测试）

    # 第四阶段「检索闭环」交付配置（冻结验收证据包绑定数值）
    # 向量/词法两路在允许范围内构造候选，RRF 融合 → 同源去重 → 规则重排 → top_k 截取。
    INDEX_EMBEDDING_MODEL: str = "text-embedding-v3"   # embedding 身份（与维度绑定）
    INDEX_EMBEDDING_DIMENSIONS: int = 1024             # 向量维度（向量列固定，错误维度拒绝）
    INDEX_CONTENT_HASH_ALGO: str = "sha256"            # 内容摘要算法（来源身份去重用）
    INDEX_CHUNK_SIZE: int = 1000                       # 单个索引块正文上限（字符）
    INDEX_CHUNK_OVERLAP: int = 0                       # 相邻索引块重叠（块级定位，无需重叠）
    INDEX_CANDIDATE_POOL: int = 20                     # 单路召回候选池大小
    INDEX_TOP_K: int = 5                               # 最终返回数量（融合/去重/重排后截取）
    INDEX_RRF_K: int = 60                              # RRF 融合常数
    INDEX_CONTEXT_BUDGET: int = 4000                   # 单结果上下文总预算（命中 + 邻接，字符）
    INDEX_SINGLE_SOURCE_LIMIT: int = 3                 # 单来源邻接块/片段上限
    INDEX_MAX_ATTEMPTS: int = 3                        # 索引构建失败重试上限
    INDEX_RETRIEVAL_RELEVANCE_THRESHOLD: float = 0.1   # 向量召回最小余弦相似度（无共享内容≈0，过滤后归 no_evidence）
    # 索引滞后策略：检索始终读取唯一 active 索引版本；构建期间旧 active 保持有效，
    # 激活为原子切换，不返回半成品（声明：无缓存，不返回“暂存中的旧结果”）。
    INDEX_LAG_STRATEGY: str = "serve_active_index"

    # 阶段级 Checkpoint 与 AI 调用控制（冻结验收 v1.1，见 docs/classroom-agent/acceptance/checkpoint-ai-control-frozen-checklist-v1.md）
    # 租约 / 心跳 / 供应商在途 permit（CA-11）
    CHECKPOINT_LEASE_SECONDS: int = 60       # 任务租约时长（秒）
    CHECKPOINT_HEARTBEAT_SECONDS: int = 10   # 心跳续租间隔（秒）
    CHECKPOINT_PROVIDER_PERMIT_MAX: int = 2  # 供应商本地在途 permit 上限
    # 控制等待与 Redis 故障（S2/S3）
    CHECKPOINT_CONTROL_WAIT_SECONDS: int = 900   # 累计控制等待上限
    CHECKPOINT_REDIS_TIMEOUT_SECONDS: float = 1.0
    CHECKPOINT_REDIS_POOL_MAX: int = 20
    CHECKPOINT_REDIS_WAIT_SECONDS: float = 5.0   # Redis 不可用每次至少等待
    # 外部请求有界重试（S3）
    CHECKPOINT_EXT_MAX_ATTEMPTS: int = 3
    CHECKPOINT_EXT_BACKOFF_BASE: float = 2.0
    CHECKPOINT_EXT_BACKOFF_CAP: float = 30.0
    CHECKPOINT_EXT_OP_TIMEOUT_SECONDS: int = 900  # 单操作自动重试 / ASR 轮询总期限
    CHECKPOINT_EXT_HTTP_TIMEOUT_SECONDS: int = 30  # 单次请求超时
    # 令牌桶（S2/S3；冻结配置是测试条件，不是容量承诺）
    # 显式开启才启用 Redis 令牌桶（入口+供应商）；关闭时完全旁路，不依赖 Redis。
    RATE_LIMIT_ENABLED: bool = False
    RATE_USER_CAPACITY: int = 3
    RATE_USER_REFILL: float = 0.1
    RATE_PROVIDER_GEN_CAPACITY: int = 2
    RATE_PROVIDER_GEN_REFILL: float = 0.5
    RATE_EMBED_CAPACITY: int = 2
    RATE_EMBED_REFILL: float = 1.0
    RATE_ASR_SUBMIT_CAPACITY: int = 1
    RATE_ASR_SUBMIT_REFILL: float = 0.1
    RATE_ASR_QUERY_CAPACITY: int = 2
    RATE_ASR_QUERY_REFILL: float = 1.0
    # 持久预算（S3；金额定点整数微元，1 元 = 1,000,000 微元，不用浮点）
    BUDGET_SUBJECT_DAILY_CNY: int = 5
    BUDGET_GLOBAL_DAILY_CNY: int = 10
    BUDGET_TRIAL_TOTAL_CNY: int = 10
    BUDGET_MICRO_PER_CNY: int = 1_000_000
    BUDGET_RESERVE_MIN_CNY: int = 1  # 每次保守预留下限（元）
    # 统一调用网关（S3）：显式开启才走网关（令牌桶 + 并发 permit + 预算 + provider_calls + 有界退避）。
    # 关闭时保持 S1/S2 既有路径（仅令牌桶），不依赖预算账户。
    CALL_GATEWAY_ENABLED: bool = False
    # 供应商在途 permit：数据库槽位可续租，过期可回收（CA-17）
    CHECKPOINT_PROVIDER_PERMIT_TTL_SECONDS: int = 60
    # ASR 正常轮询最小间隔（秒；CA-22）
    CHECKPOINT_ASR_POLL_INTERVAL_SECONDS: int = 5

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]


settings = Settings()
