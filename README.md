# 课堂整理与问答 Agent

## 项目简介

面向个人学习场景的课堂助手，将课堂录音与课件整理为可回溯的学习资料。

- 管理课程和课时，上传课堂录音与课件。
- 转写录音并保留时间戳，点击转写段即可回听。
- 整理课堂笔记、知识点、例题和重点难点，检查草稿后确认发布。
- 在课程或课时范围内提问，通过引用打开笔记、课件原文或对应录音。

前端采用 React、TypeScript 和 Vite，后端采用 FastAPI，使用 PostgreSQL/pgvector 与 Redis。模型通过 OpenAI 兼容接口接入，真实语音转写使用 DashScope，音频存储使用私有 OSS。

## 启动方式

以下命令适用于 Windows PowerShell。需要安装 Python 3.12、Node.js/npm 和 Docker Desktop。

### 首次配置

先启动 Docker Desktop，在项目根目录执行：

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

在本机 backend/.env 中填写模型密钥、DashScope 和私有 OSS 配置。设置 ASR_MODE=real、INDEX_EMBEDDING_PROVIDER=real，并在确认允许供应商计费后设置 CLASSROOM_PAID_CALLS_ENABLED=true。具体配置项见 backend/.env.example，实际密钥保存在本机 .env 中。

初始化课堂数据库：

~~~powershell
cd backend
.\.venv\Scripts\python.exe scripts/classroom_local.py init
.\.venv\Scripts\python.exe scripts/classroom_local.py status
cd ..
~~~

### 日常启动

启动 Docker Desktop 后，在项目根目录执行：

~~~powershell
docker compose up -d
.\scripts\start-classroom.ps1
~~~

脚本会启动前端、后端 API 和后台任务进程。

- 课程首页：http://localhost:5173/
- 课堂问答：http://localhost:5173/classroom-qa
- API 文档：http://localhost:8787/docs

如果 PowerShell 禁止执行脚本，可运行：

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\start-classroom.ps1
~~~

### 停止服务

~~~powershell
.\scripts\start-classroom.ps1 -Stop
~~~

如需同时停止数据库和 Redis：

~~~powershell
docker compose stop
~~~

启动日志位于 backend/runtime/。当前默认配置面向本机个人使用。

