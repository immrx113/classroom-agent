"""种子数据脚本：插入完整的示例数据，让前端直接展示效果

包含 3 个会议示例：
1. Q3 产品规划讨论（已生成纪要）
2. 技术架构评审会（已生成纪要）
3. 周例会（待生成纪要）

用法：cd backend && PYTHONPATH=. .venv/bin/python seed_data.py
"""

import asyncio
import sys
import uuid
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/Users/a95722807/Documents/trae_projects/yuan-meet/backend')

from sqlalchemy import select, delete
from app.db.session import async_session_factory
from app.models.meeting import Meeting
from app.models.transcript import Transcript
from app.models.summary import Summary
from app.models.action_item import ActionItem
from app.models.risk import Risk
from app.services.knowledge_service import knowledge_service


# ──────────────────────────────────────────────────────────────
# 会议 1：Q3 产品规划讨论
# ──────────────────────────────────────────────────────────────
MEETING_1 = {
    "title": "Q3 产品规划讨论",
    "description": "讨论 Q3 季度产品路线图、优先级排序及资源分配",
    "participants": ["张三", "李四", "王五", "赵六"],
}

TRANSCRIPTS_1 = [
    (0, "张三", "大家好，今天我们讨论 Q3 的产品规划。先回顾一下 Q2 的完成情况。"),
    (10, "李四", "Q2 我们完成了移动端 App 1.0 版本上线，用户量达到 5 万。但留存率只有 35%，低于预期。"),
    (22, "张三", "确实，Q3 的重点应该放在提升留存率上。我建议优先做用户反馈驱动的功能迭代。"),
    (33, "王五", "我同意。另外我们还需要优化新用户引导流程，目前转化率太低。"),
    (45, "赵六", "从数据看，新用户在第 3 步流失率最高，达到 60%。建议简化注册流程。"),
    (58, "张三", "好的，王五负责用户引导优化，赵六负责注册流程简化。两个项目都需要在 7 月底前完成。"),
    (72, "李四", "我担心同时推进两个项目会导致资源紧张，前端开发只有 2 个人。"),
    (83, "张三", "这是个风险，但我们可以通过外包解决一部分。李四你来跟进外包事宜。"),
    (95, "张三", "最后，我们需要建立数据监控体系，每周 review 关键指标。由我负责。"),
    (108, "张三", "会议到此结束，主要结论是 Q3 聚焦留存率提升，三个关键项目并行推进。"),
]

SUMMARY_1 = """## 会议概述

本次会议讨论了 Q3 产品规划方向，重点聚焦用户留存率提升。基于 Q2 数据复盘（留存率 35% 低于预期），确定了三个关键项目并行推进。

## 讨论要点

### Q2 复盘
- 移动端 App 1.0 上线，用户量达 5 万
- 留存率 35%，低于 40% 的预期目标

### Q3 优先级
1. **用户反馈驱动迭代**：基于用户反馈优化核心功能
2. **新用户引导优化**：当前转化率偏低，第 3 步流失率高达 60%
3. **注册流程简化**：降低新用户进入门槛

### 资源安排
- 前端开发资源紧张（仅 2 人），考虑外包补充
- 建立每周数据 review 机制

## 会议结论

Q3 聚焦留存率提升，三个关键项目并行推进：
- 用户引导优化（王五负责）
- 注册流程简化（赵六负责）
- 外包资源协调（李四负责）
- 数据监控体系（张三负责）
"""

KEY_POINTS_1 = [
    "Q2 留存率 35% 低于 40% 预期",
    "新用户第 3 步流失率 60%",
    "Q3 三个关键项目并行推进",
    "前端资源紧张需外包补充",
    "建立每周数据 review 机制",
]

ACTION_ITEMS_1 = [
    {"title": "优化新用户引导流程", "assignee": "王五", "due_date": "2026-07-31", "priority": "high"},
    {"title": "简化注册流程（降低第 3 步流失）", "assignee": "赵六", "due_date": "2026-07-31", "priority": "high"},
    {"title": "协调前端外包资源", "assignee": "李四", "due_date": "2026-07-15", "priority": "medium"},
    {"title": "搭建每周数据监控看板", "assignee": "张三", "due_date": "2026-07-10", "priority": "medium"},
]

RISKS_1 = [
    {"description": "前端开发资源不足（仅 2 人），同时推进两个项目可能导致延期", "severity": "high", "mitigation": "通过外包补充前端开发资源，优先保障关键路径"},
    {"description": "新用户引导优化效果未经验证，可能需要多轮迭代", "severity": "medium", "mitigation": "采用 A/B 测试验证方案，设置 2 周验证周期"},
]


# ──────────────────────────────────────────────────────────────
# 会议 2：技术架构评审会
# ──────────────────────────────────────────────────────────────
MEETING_2 = {
    "title": "技术架构评审会 - 微服务拆分",
    "description": "评审单体架构向微服务迁移的技术方案，讨论服务边界、数据一致性和部署策略",
    "participants": ["陈工", "刘工", "周工", "吴工"],
}

TRANSCRIPTS_2 = [
    (0, "陈工", "今天评审微服务拆分方案。目前单体架构已经无法支撑业务增长，QPS 峰值 5000 时响应时间超过 2 秒。"),
    (15, "刘工", "我建议按业务域拆分：用户服务、订单服务、支付服务、商品服务。"),
    (28, "周工", "拆分后数据一致性是个大问题。订单和支付之间需要分布式事务。"),
    (40, "吴工", "可以考虑用 Saga 模式，配合消息队列实现最终一致性。"),
    (52, "陈工", "同意。另外服务间通信用 gRPC，内部调用性能更好。"),
    (65, "刘工", "部署方面，建议用 K8s + Istio，支持灰度发布和熔断。"),
    (78, "周工", "监控体系也要跟上，Prometheus + Grafana + Jaeger 链路追踪。"),
    (90, "陈工", "迁移策略上，我建议采用绞杀者模式，逐步迁移而非一次性重构。"),
    (103, "刘工", "时间安排：第一阶段用户服务迁移，预计 8 月完成。"),
    (115, "陈工", "好，刘工负责用户服务，周工负责支付服务，吴工负责基础设施搭建。我在 8 月 15 日前完成架构文档。"),
    (128, "陈工", "最大的风险是迁移过程中影响线上服务，需要做好回滚预案。"),
]

SUMMARY_2 = """## 会议概述

评审单体架构向微服务迁移方案。当前单体架构在 QPS 5000 峰值时响应超 2 秒，已无法支撑业务增长。会议确定了服务拆分边界、技术选型、迁移策略和人员分工。

## 讨论要点

### 现状问题
- 单体架构 QPS 峰值 5000 时响应时间 > 2 秒
- 无法独立扩展，影响迭代效率

### 服务拆分方案
按业务域拆分为 4 个核心服务：
1. **用户服务** - 账号、权限、个人中心
2. **订单服务** - 订单生命周期管理
3. **支付服务** - 支付与退款
4. **商品服务** - 商品信息与库存

### 技术选型
| 领域 | 选型 | 理由 |
|---|---|---|
| 服务通信 | gRPC | 内部调用性能优于 HTTP |
| 数据一致性 | Saga + 消息队列 | 最终一致性，避免强一致性能瓶颈 |
| 部署 | Kubernetes + Istio | 灰度发布、熔断、流量管控 |
| 监控 | Prometheus + Grafana + Jaeger | 指标 + 看板 + 链路追踪 |

### 迁移策略
采用**绞杀者模式**（Strangler Pattern），逐步迁移而非一次性重构

## 会议结论

微服务拆分启动，第一阶段迁移用户服务（8 月完成）：
- 用户服务迁移：刘工
- 支付服务迁移：周工
- 基础设施搭建：吴工
- 架构文档输出：陈工（8 月 15 日前）
"""

KEY_POINTS_2 = [
    "单体架构 QPS 5000 响应超 2 秒，需微服务化",
    "按业务域拆分 4 个核心服务",
    "gRPC + Saga + K8s/Istio 技术栈",
    "采用绞杀者模式逐步迁移",
    "第一阶段用户服务 8 月完成",
]

ACTION_ITEMS_2 = [
    {"title": "完成微服务架构设计文档", "assignee": "陈工", "due_date": "2026-08-15", "priority": "high"},
    {"title": "用户服务微服务拆分与迁移", "assignee": "刘工", "due_date": "2026-08-31", "priority": "high"},
    {"title": "搭建 K8s + Istio 基础设施", "assignee": "吴工", "due_date": "2026-07-20", "priority": "high"},
    {"title": "支付服务 Saga 模式实现", "assignee": "周工", "due_date": "2026-09-15", "priority": "medium"},
    {"title": "搭建 Prometheus + Grafana 监控体系", "assignee": "吴工", "due_date": "2026-07-25", "priority": "medium"},
]

RISKS_2 = [
    {"description": "迁移过程中可能影响线上服务稳定性", "severity": "high", "mitigation": "采用绞杀者模式逐步迁移，每个服务迁移前制定回滚预案，灰度发布验证"},
    {"description": "分布式事务实现复杂，Saga 模式开发周期较长", "severity": "medium", "mitigation": "支付服务优先用本地消息表方案过渡，后续再升级为完整 Saga"},
    {"description": "团队对 K8s/Istio 经验不足，基础设施搭建可能延期", "severity": "medium", "mitigation": "安排培训 + 引入外部专家咨询，预留 1 周缓冲期"},
]


# ──────────────────────────────────────────────────────────────
# 会议 3：周例会（待生成纪要，展示 pending 状态）
# ──────────────────────────────────────────────────────────────
MEETING_3 = {
    "title": "研发周例会 - 0624",
    "description": "本周工作同步与下周计划",
    "participants": ["张三", "李四", "王五"],
}

TRANSCRIPTS_3 = [
    (0, "张三", "开始周例会。先同步本周进度。"),
    (10, "李四", "本周完成了用户反馈系统上线，收集了 200 条反馈。"),
    (22, "王五", "新用户引导 A/B 测试第一轮完成，B 组转化率提升 15%。"),
    (35, "张三", "不错。下周重点：李四继续优化反馈系统，王五推进引导流程 B 组全量。"),
    (50, "张三", "散会。"),
]


# ──────────────────────────────────────────────────────────────
# 知识库文档：企业内部业务知识（用于 RAG 检索演示）
# ──────────────────────────────────────────────────────────────
KNOWLEDGE_DOCS = [
    {
        "title": "产品研发流程规范",
        "category": "研发管理",
        "content": """# 产品研发流程规范

## 1. 概述
本规范定义了公司产品研发的标准流程，适用于所有产品线的版本迭代。

## 2. 研发阶段

### 2.1 需求评审
- 产品经理输出 PRD（产品需求文档），包含用户故事、验收标准
- 研发、设计、测试三方参与评审，会议时长不超过 60 分钟
- 评审通过后，需求进入待开发池，按优先级排期

### 2.2 技术方案设计
- 复杂需求（预估工作量 > 5 人日）需输出技术方案文档
- 技术方案包含：架构图、数据库设计、接口定义、风险评估
- 由技术负责人评审，48 小时内反馈

### 2.3 编码开发
- 采用 Git Flow 分支模型：feature/* → develop → release → main
- 代码提交前需通过本地单元测试（覆盖率 >= 70%）
- 提交信息遵循 Conventional Commits 规范（feat/fix/docs/refactor）

### 2.4 代码评审（Code Review）
- 每个 PR 至少 1 人评审，核心模块需 2 人评审
- 评审重点：业务逻辑、性能、安全、代码可读性
- 评审意见 24 小时内响应

### 2.5 测试验证
- 测试用例由 QA 编写，覆盖功能测试、边界测试、回归测试
- 严重级别 P0/P1 缺陷必须修复后才能发布
- 性能测试：接口 P99 延迟 < 500ms，吞吐量 > 200 QPS

### 2.6 发布部署
- 发布窗口：每周二、周四 14:00-18:00
- 采用灰度发布：先 10% 流量，观察 30 分钟无异常后全量
- 回滚机制：任何异常可在 5 分钟内回滚到上一版本

## 3. 研发效能指标
- 需求交付周期：< 14 天（从评审通过到上线）
- 缺陷逃逸率：< 5%（线上缺陷 / 总缺陷）
- 发布成功率：> 95%

## 4. 技术栈规范
- 后端：Python 3.11 + FastAPI + PostgreSQL
- 前端：React 18 + TypeScript + Vite
- 移动端：React Native 0.72
- 基础设施：Docker + Kubernetes + 阿里云 ACK
""",
    },
    {
        "title": "数据安全与隐私保护规范",
        "category": "安全合规",
        "content": """# 数据安全与隐私保护规范

## 1. 适用范围
本规范适用于公司所有涉及用户数据的系统，包括但不限于用户画像、行为日志、交易记录。

## 2. 数据分级

### 2.1 机密数据（L4）
- 用户身份证号、银行卡号、密码
- 存储：必须加密（AES-256），传输：必须 TLS 1.2+
- 访问：需审批 + 双因素认证，操作日志保留 2 年

### 2.2 敏感数据（L3）
- 手机号、邮箱、住址、位置信息
- 存储：可逆加密或脱敏（中间 4 位掩码）
- 查询：接口返回必须脱敏，禁止全量导出

### 2.3 内部数据（L2）
- 用户行为日志、设备信息、偏好设置
- 存储：明文，但需访问控制
- 使用：可用于数据分析，但不可对外暴露

### 2.4 公开数据（L1）
- 商品信息、公告、公开内容
- 无特殊保护要求

## 3. 隐私合规要求

### 3.1 用户授权
- 收集用户数据前必须获得明示同意
- 隐私政策需明确告知数据用途、存储期限、第三方共享情况
- 用户有权随时撤回授权并要求删除数据（GDPR/PIPL）

### 3.2 数据脱敏
- 日志中禁止打印用户敏感信息
- 测试环境数据必须从生产环境脱敏后同步
- 数据库导出需自动脱敏处理

### 3.3 数据留存
- 用户行为日志：保留 6 个月
- 交易记录：保留 5 年（合规要求）
- 已注销用户数据：30 天内物理删除

## 4. 安全审计
- 每月进行一次权限审计，清理离职人员账号
- 每季度进行一次渗透测试
- 安全事件响应：P0 级事件 15 分钟内响应，1 小时内止损
""",
    },
    {
        "title": "用户增长策略与运营手册",
        "category": "产品运营",
        "content": """# 用户增长策略与运营手册

## 1. 北极星指标
公司当前北极星指标为「周活跃用户留存率」，目标值 45%。

## 2. 用户分层模型（RFM）

### 2.1 R（Recency 最近活跃）
- R1：7 天内活跃（高价值）
- R2：8-30 天活跃（中价值）
- R3：30 天以上未活跃（流失风险）

### 2.2 F（Frequency 活跃频次）
- F1：周活跃 >= 5 次
- F2：周活跃 2-4 次
- F3：周活跃 1 次

### 2.3 M（Monetary 付费金额）
- M1：累计付费 > 500 元
- M2：累计付费 100-500 元
- M3：未付费或 < 100 元

### 2.4 分层运营策略
- R1F1M1（核心用户）：专属客服、新功能内测、年度礼包
- R2F2M2（成长用户）：优惠券激励、功能引导
- R3F3M3（流失用户）：召回 push、限时折扣

## 3. 新用户激活路径

### 3.1 注册转化优化
- 注册流程从 5 步简化为 3 步（手机号 → 验证码 → 设置密码）
- 第三方登录（微信、QQ）转化率比手机号高 40%
- 新用户首日激活率目标：60%

### 3.2 Aha Moment 引导
- 首次完成核心操作（创建会议）后触发庆祝动效
- 第二日推送使用 tips，引导探索高级功能
- 第七日发送「每周总结」，展示用户使用数据

## 4. 留存策略

### 4.1 次日留存（目标 50%）
- 新用户注册后 24 小时内推送个性化内容
- 首次使用未完成核心流程的用户，次日 push 引导继续

### 4.2 7 日留存（目标 35%）
- 每日签到奖励（积分 + 连续签到加成）
- 周三推送「本周精选」内容

### 4.3 30 日留存（目标 25%）
- 月度账单：展示用户本月使用统计
- 付费转化：免费用户 30 天后触发限时优惠

## 5. 增长实验机制
- 每周至少 2 个 A/B 测试上线
- 实验周期 >= 7 天，样本量 >= 10000
- 显著性阈值 p < 0.05
- 实验结论沉淀到增长知识库
""",
    },
    {
        "title": "微服务架构设计指南",
        "category": "技术架构",
        "content": """# 微服务架构设计指南

## 1. 架构原则

### 1.1 单一职责
每个微服务只负责一个业务领域，例如：用户服务、订单服务、支付服务。

### 1.2 服务自治
- 每个服务拥有独立的数据库
- 服务间通过 API 通信，禁止直接访问对方数据库
- 独立部署、独立扩缩容

### 1.3 去中心化
- 避免强依赖的调用链路（A → B → C → D）
- 使用消息队列解耦异步流程
- 读写分离：查询走缓存或只读库

## 2. 服务拆分标准

### 2.1 拆分粒度
- 单服务代码量 < 5 万行
- 团队规模 2-5 人负责一个服务
- 单服务 QPS < 5000（超出则需进一步拆分）

### 2.2 拆分依据
- 按业务领域拆分（DDD 领域驱动设计）
- 按变更频率拆分（高频变更与稳定模块分离）
- 按性能要求拆分（计算密集型独立部署）

## 3. 通信协议

### 3.1 同步调用
- 服务间：gRPC（性能高，Protobuf 序列化）
- 对外 API：RESTful HTTP（通用性强）
- 超时设置：连接超时 1s，读取超时 3s

### 3.2 异步消息
- 消息中间件：Apache RocketMQ / Kafka
- 消息可靠性：至少一次投递（At Least Once）
- 幂等性：消费端必须实现幂等处理

## 4. 数据一致性

### 4.1 最终一致性
- 跨服务事务采用 Saga 模式
- 每个本地事务有对应的补偿操作
- 通过消息驱动状态流转

### 4.2 分布式事务（极少使用）
- 仅用于金融级强一致性场景
- 使用 Seata AT 模式
- 性能开销大，避免滥用

## 5. 服务治理

### 5.1 服务注册与发现
- 注册中心：Nacos
- 健康检查：心跳间隔 5s，失败阈值 3 次
- 优雅下线：先标记不健康，等待 30s 后下线

### 5.2 熔断降级
- 熔断阈值：错误率 > 50% 持续 10s
- 熔断恢复：半开状态试探，成功率达 80% 后关闭
- 降级策略：返回缓存数据或默认值

### 5.3 限流
- 网关层：令牌桶算法，全局限流
- 服务层：基于 QPS 的并发控制
- 热点数据：单 key 限流

## 6. 可观测性
- 日志：ELK（Elasticsearch + Logstash + Kibana）
- 指标：Prometheus + Grafana
- 链路追踪：SkyWalking / Jaeger
- 告警：核心指标 P99 延迟 > 1s 告警
""",
    },
    {
        "title": "新人入职指南",
        "category": "行政管理",
        "content": """# 新人入职指南

## 1. 入职流程

### 1.1 入职前准备（HR 负责）
- 发送 offer letter 及入职须知
- 准备办公设备：MacBook Pro、显示器、键鼠套装
- 创建企业邮箱、钉钉账号、VPN 账号
- 通知用人部门负责人

### 1.2 入职当天
- 09:00 HR 接待，签订劳动合同
- 09:30 领取设备，配置开发环境
- 10:00 部门负责人介绍团队成员
- 14:00 IT 培训：账号权限、开发规范、安全要求
- 16:00 导师（Buddy）对接，开始熟悉项目

## 2. 开发环境配置

### 2.1 必装软件
- IDE：VS Code / WebStorm / PyCharm（按技术栈）
- 终端：iTerm2 + Oh My Zsh
- Git：配置 SSH key 并添加到 GitLab
- Docker Desktop：用于本地运行依赖服务
- Postman：API 调试

### 2.2 项目克隆与启动
```bash
# 克隆仓库
git clone git@gitlab.company.com:team/yuan-meet.git
cd yuan-meet

# 后端
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # 配置环境变量
python -m app.main

# 前端
cd frontend
npm install
npm run dev
```

## 3. 常用内部系统
- GitLab：代码仓库 gitlab.company.com
- 钉钉：日常沟通、审批
- Confluence：文档 wiki
- Jira：需求与缺陷管理
- Grafana：监控大盘
- 阿里云控制台：云资源管理

## 4. 福利待遇
- 弹性工作：10:00-19:00（核心工作时间 11:00-18:00）
- 年假：入职即享 5 天，满 1 年 10 天，满 3 年 15 天
- 餐补：30 元/天
- 下午茶：每周二、周四
- 团建：每季度 1 次，预算 200 元/人

## 5. 试用期考核
- 试用期 3 个月
- 考核维度：技术能力（40%）、协作沟通（30%）、产出质量（30%）
- 转正答辩：准备 PPT，向部门负责人 + HR 汇报
""",
    },
]


async def seed():
    """插入种子数据"""
    async with async_session_factory() as db:
        # 清除旧数据（保留现有的对话会话）
        print("清除旧的会议数据...")
        await db.execute(delete(Risk))
        await db.execute(delete(ActionItem))
        await db.execute(delete(Summary))
        await db.execute(delete(Transcript))
        await db.execute(delete(Meeting))
        # 清除知识库中会议纪要类型的文档
        from app.models.knowledge_doc import KnowledgeDocument
        await db.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.source_type == "meeting_summary"
            )
        )
        await db.flush()
        print("已清除旧数据\n")

        meetings_data = [
            (MEETING_1, TRANSCRIPTS_1, SUMMARY_1, KEY_POINTS_1, ACTION_ITEMS_1, RISKS_1),
            (MEETING_2, TRANSCRIPTS_2, SUMMARY_2, KEY_POINTS_2, ACTION_ITEMS_2, RISKS_2),
            (MEETING_3, TRANSCRIPTS_3, None, None, None, None),  # 无纪要
        ]

        now = datetime.now(timezone.utc)

        for idx, (m_data, transcripts, summary_text, key_points, actions, risks) in enumerate(meetings_data):
            # 创建会议（时间错开，便于排序）
            meeting = Meeting(
                title=m_data["title"],
                description=m_data["description"],
                participants=m_data["participants"],
                status="processed" if summary_text else "pending",
                start_time=now - timedelta(days=3 - idx, hours=2),
                end_time=now - timedelta(days=3 - idx, hours=1),
            )
            db.add(meeting)
            await db.flush()
            print(f"✓ 创建会议: {m_data['title']} (id={meeting.id})")

            # 插入转写
            for seq, speaker, content in transcripts:
                db.add(Transcript(
                    meeting_id=meeting.id,
                    speaker=speaker,
                    content=content,
                    start_time=float(seq),
                    end_time=float(seq + 8),
                    seq_index=seq // 10 if seq >= 10 else seq,
                ))
            print(f"  └─ 插入 {len(transcripts)} 条转写")

            # 插入纪要
            if summary_text:
                summary = Summary(
                    meeting_id=meeting.id,
                    content=summary_text,
                    key_points=key_points,
                    status="completed",
                )
                db.add(summary)

                # 插入行动项
                for item in actions:
                    from datetime import date
                    due = date.fromisoformat(item["due_date"])
                    db.add(ActionItem(
                        meeting_id=meeting.id,
                        title=item["title"],
                        assignee=item["assignee"],
                        due_date=due,
                        priority=item["priority"],
                        status="pending",
                    ))

                # 插入风险
                for r in risks:
                    db.add(Risk(
                        meeting_id=meeting.id,
                        description=r["description"],
                        severity=r["severity"],
                        mitigation=r["mitigation"],
                    ))

                print(f"  └─ 插入纪要 + {len(actions)} 行动项 + {len(risks)} 风险")

                # 索引到知识库
                try:
                    await knowledge_service.index_meeting_summary(
                        db=db,
                        meeting_id=meeting.id,
                        meeting_title=meeting.title,
                        summary_content=summary_text,
                    )
                    print(f"  └─ 已索引到知识库")
                except Exception as e:
                    print(f"  └─ 知识库索引失败（不影响）: {e}")

            await db.flush()

        # ──────────────────────────────────────────────────────
        # 知识库文档：企业内部业务知识
        # ──────────────────────────────────────────────────────
        print("\n插入知识库文档...")
        # 先清除旧的 uploaded_doc 类型
        from app.models.knowledge_doc import KnowledgeDocument
        await db.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.source_type == "uploaded_doc"
            )
        )
        await db.flush()

        for doc in KNOWLEDGE_DOCS:
            try:
                await knowledge_service.index_text(
                    db=db,
                    title=doc["title"],
                    content=doc["content"],
                    source_type="uploaded_doc",
                    metadata={"filename": doc["title"] + ".md", "category": doc.get("category", "通用")},
                )
                print(f"  └─ 已索引: {doc['title']}")
            except Exception as e:
                print(f"  └─ 索引失败 {doc['title']}: {e}")

        await db.commit()
        print(f"\n✅ 种子数据插入完成！共 {len(meetings_data)} 个会议，{len(KNOWLEDGE_DOCS)} 篇知识文档")
        print("\n现在可以打开前端查看效果：")
        print("  - 会议列表：http://localhost:5173/meetings")
        print("  - 纪要列表：http://localhost:5173/summaries")
        print("  - 知识库：  http://localhost:5173/knowledge")


if __name__ == "__main__":
    asyncio.run(seed())
