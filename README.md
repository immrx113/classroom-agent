# 课堂整理与问答 Agent

个人单用户课堂学习助手。支持课程与课时管理、录音上传与转写、课件解析、课堂笔记/知识点/例题/重点难点整理、草稿确认发布，以及带来源定位的课程问答。

技术栈包括 React、TypeScript、Vite、FastAPI、PostgreSQL/pgvector、Redis。模型通过 OpenAI 兼容接口接入，真实转写使用 DashScope，音频存储使用私有 OSS。

## 日常启动（已配置的 Windows 电脑）

先启动 Docker Desktop，在项目根目录运行：

~~~powershell
docker compose up -d
.\scripts\start-classroom.ps1
~~~

打开 [课程首页](http://localhost:5173/) 或 [课堂问答](http://localhost:5173/classroom-qa)。API 文档位于 [本地接口页](http://localhost:8787/docs)。

页面流程为创建课程和课时、上传录音及课件、等待转写、整理本课、检查草稿、确认发布、提问和点击引用。

停止应用：

~~~powershell
.\scripts\start-classroom.ps1 -Stop
~~~

此命令停止前端、API 和后台任务。Docker 数据库及 Redis 可用 docker compose stop 停止。

PowerShell 禁止脚本执行时，可仅对本次进程运行：

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-classroom.ps1
~~~

运行日志位于 backend/runtime/。首次真实任务可能耗时较长，启动成功表示服务可用。

## 首次安装

需要 Python 3.12、Node.js/npm 和 Docker Desktop。在项目根目录依次执行：

~~~powershell
docker compose up -d
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt mutagen
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
cd ../frontend
npm ci
cd ..
~~~

在本机 backend/.env 填写配置：

- OPENAI_API_KEY、OPENAI_BASE_URL、LLM_MODEL 用于模型调用。
- ASR_MODE=real，按需要设置 DASHSCOPE_API_KEY；现有接入可复用模型密钥。
- OSS_ACCESS_KEY_ID、OSS_ACCESS_KEY_SECRET、OSS_ENDPOINT、OSS_BUCKET_NAME 用于私有音频存储。
- INDEX_EMBEDDING_PROVIDER=real 及对应模型配置用于向量检索。
- CLASSROOM_PAID_CALLS_ENABLED=true 表示允许真实付费调用。模板默认关闭，配置并确认费用后开启。

初始化本机独立课堂库，然后返回根目录启动：

~~~powershell
cd backend
.\.venv\Scripts\python.exe scripts/classroom_local.py init
.\.venv\Scripts\python.exe scripts/classroom_local.py status
cd ..
.\scripts\start-classroom.ps1
~~~

当前 Docker Compose 的数据库口令为本机开发默认值。对外部署前应调整数据库凭据、网络访问和身份认证；本版本按本机个人单用户使用验收。

## 凭据与上传范围

实际密钥仅放 backend/.env，模板只保留空值。前端不接收供应商密钥，避免把密钥放进 VITE_ 环境变量或提交到源码。

.gitignore 排除了 .env、上传资料、运行目录、日志、依赖、构建结果及本机原始验收证据。上传 GitHub 应使用 Git 提交并检查暂存文件。网页直接上传或压缩包不会应用这些忽略规则。

若密钥曾提交或公开，应在供应商平台撤销并更换，同时处理仓库历史。

## 文档与验证范围

- [当前需求](docs/classroom-agent/reset-v1/01-requirements.md)
- [修改方案](docs/classroom-agent/reset-v1/02-plan.md)
- [冻结验收清单 v1.2](docs/classroom-agent/reset-v1/03-acceptance-v1.2.md)
- [最终验收报告](docs/classroom-agent/reset-v1/17-final-review-v1.2.md)
- [文档导航](docs/classroom-agent/README.md)

本版本按个人单用户范围完成 A1–A7 验收。真实 ASR 已验证；整理、问答及嵌入的自动回归使用替身或确定性实现，真实模型效果尚未完整验证。历史迁移采用构造样本。原始验收日志和截图保留在本机，仓库中的报告可能引用这些未上传的证据。

旧会议代码及兼容入口保留在项目中。当前课程功能从首页进入。


