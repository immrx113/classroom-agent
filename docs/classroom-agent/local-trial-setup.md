# 当前电脑首次真实试用

本轮使用阿里云qwen-plus、Paraformer-v2、text-embedding-v3；用户已批准10元试用上限。先跑45秒教学录音，不需要提供真实课堂资料。

## 当前已经完成（2026-10-30更新）

- qwen-plus真实调用成功：从短教学文本提取1条可核对原文的结论、1项作业。
- text-embedding-v3真实调用成功：2条1024维向量。
- 新课堂索引及查询默认使用真实embedding，不再默认用本地替身；测试脚本显式使用替身。
- 私有OSS签名读取链接、后台任务入口、独立课堂库初始化及一轮试用入口已提供。
- 本地契约测试6项通过；隔离库“上传→转写→笔记→索引”及二轮幂等验证通过（模型用替身）；第四阶段19条回归通过。
- OSS私有上传、真实Paraformer转写、qwen整理、真实embedding索引及检索已串联通过；真实浏览器原文打开和固定资产回听通过。
- 原正式库未升级；当前本机配置已切到独立试用库yuan_classroom_local。

## 1. 开通OSS并创建一个Bucket

1. 登录[OSS控制台](https://oss.console.aliyun.com/)，按提示开通服务。
2. 在Bucket列表创建Bucket，名称自行取一个全局唯一名称，例如`yuan-classroom-你的后缀`。
3. 本轮采用华北2（北京）、标准存储、私有读写；保留阻止公共访问。其他不需要的功能保持默认，不必为短录音先购买大容量资源包。
4. 创建完成后，在概览/访问域名处记录Bucket名称和**外网Endpoint**，北京示例为`https://oss-cn-beijing.aliyuncs.com`，不要使用带`-internal`的内网Endpoint。
5. 可设置`audio/`前缀一天后过期的生命周期规则，用于进程异常时清理遗留临时录音。正常完成后程序会尝试删除临时对象。

开通服务本身免费，实际文件存储、请求和流出流量可能计费，见[官方控制台入门](https://help.aliyun.com/zh/oss/user-guide/get-started-with-oss/)。私有对象采用临时签名URL授权ASR读取，不需要开放公共读权限，见[官方私有资源访问说明](https://www.alibabacloud.com/help/zh/oss/how-to-apply-the-private-permission-to-the-actual-business)。

## 2. 创建专用RAM程序用户并授权

1. 进入[RAM控制台](https://ram.console.aliyun.com/)，创建一个专用程序用户，例如`yuan-classroom-local`，使用程序AccessKey访问。
2. 给它添加下面的自定义权限策略，将`你的Bucket名称`替换为实际名称；只允许本桶`audio/`路径的上传、读取、删除，不需要授予整个OSS的管理权限。
3. 保存AccessKey ID和AccessKey Secret，只填本机配置，不发送到聊天，也不要提交到仓库。Secret通常只在创建时显示，详见[官方RAM用户创建说明](https://help.aliyun.com/zh/ram/user-guide/create-a-ram-user)。

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:PutObject", "oss:GetObject", "oss:DeleteObject"],
      "Resource": ["acs:oss:*:*:你的Bucket名称/audio/*"]
    }
  ]
}
```

## 3. 填入本机backend/.env

保留已有OPENAI_API_KEY和DashScope配置，只补齐或修改下面的项。不要复制`.env.example`覆盖已有密钥。

```dotenv
OSS_ACCESS_KEY_ID=你的RAM用户AccessKey_ID
OSS_ACCESS_KEY_SECRET=你的RAM用户AccessKey_Secret
OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com
OSS_BUCKET_NAME=你的Bucket名称
OSS_PREFIX=audio/
ASR_MODE=real
ORGANIZE_MODEL=qwen-plus
INDEX_EMBEDDING_PROVIDER=real
INDEX_EMBEDDING_MODEL=text-embedding-v3
INDEX_EMBEDDING_DIMENSIONS=1024
CLASSROOM_PAID_CALLS_ENABLED=true
CLASSROOM_TRIAL_BUDGET_CNY=10
```

填好后只需在聊天回复“OSS已配置”，不需要发送配置文件或密钥。接下来可由助手继续初始化、联调和启动。

## 4. 独立数据库及首次试用

以下命令在项目backend目录运行。`init`只创建/升级`yuan_classroom_local`，成功后更新本机.env的DATABASE_URL；不升级、清空或写入原`yuan_meet`。运行前停止使用旧配置的后端，完成后重新启动。

```powershell
.\.venv\Scripts\python.exe scripts/classroom_local.py status
.\.venv\Scripts\python.exe scripts/classroom_local.py init
.\.venv\Scripts\python.exe scripts/classroom_local.py trial
```

trial用本地生成的`docs/classroom-agent/trial-materials/newton-trial.wav`（45.28秒、约2MB）和同目录课件，真实上传、转写、整理、发布笔记、建立索引并检索；本地状态保留在`backend/runtime/classroom-trial-state.json`供续跑，不默认每次创建一套新数据。失败任务需核对原因后显式重试；不要删除状态文件重跑来绕过幂等或预算。

## 5. 日常本机启动

当前已在本机后台运行。在项目根目录可使用以下入口重新启动或停止；启动前会核对独立库和配置，端口已被占用时不会终止未知服务。

```powershell
.\scripts\start-classroom.ps1
.\scripts\start-classroom.ps1 -Stop
```

检索页：`http://localhost:5173/retrieval`。
已生成的真实笔记：`http://localhost:5173/notes/1c489cb2-5b3d-44a2-b513-13a1210d14f3`。

也可按下面方式手动启动，不要同时再运行启动脚本：

在backend目录分别启动API及后台任务：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8787
.\.venv\Scripts\python.exe -m app.classroom_worker
```

在frontend目录运行`npm run dev -- --host 127.0.0.1`，打开`http://localhost:5173/retrieval`。首次trial成功后，笔记地址为`http://localhost:5173/notes/{trial输出的note_id}`。

worker将已完成转写自动送入整理，已发布笔记通过索引意向交给检索服务；只有一个worker能取得数据库锁。首次trial已在同一锁下执行一轮worker，不要另开脚本绕过该入口。原会议入口和新课时入口是兼容期的两套入口，本轮试用通过上述trial命令创建课时，不把旧会议上传页面当作新链路已完成的证明。

## 费用与首轮边界

- 10元是本轮上限，不是预计每次费用。每个收费请求先在本机SQLite账本预留1元；收到真实usage后按高于已批准短请求模型公开费率的保守口径核算占用，释放多余预留。缺用量/失败/超时仍保留完整1元，进程重启不清零。历史记录全部保留；可用status查看累计保守占用，不能将它当作实际扣费。2026-10-30本轮真实链路验证完成后，该值为0.18163元。实际金额以阿里云账单为准。
- 首轮只允许qwen-plus、text-embedding-v3、paraformer-v2；ASR限制120秒/8MB，整理单请求限制输入16KB及输出600 token，embedding单段8KB、单请求32KB。它们是本轮试用约束，不是完整课堂时长的最终产品标准。
- 账本保护的是新课堂链路的模型请求，不是阿里云账户级硬限额；OSS实际费用、旧聊天/会议功能及其他程序的调用不受此账本控制。本轮不要同时使用旧AI入口试费，也不要删除账本重置额度。
- 模型价格参考[阿里云官方价格页](https://help.aliyun.com/zh/model-studio/model-pricing)；实际地域、免费额度及账单以控制台为准。首次短录音旨在先验证链路，不先处理长课。
- 当前整理会抽取真实模型要点并保留原文引用，再由程序装配为笔记；章节结构的复杂语义组织尚未扩展。检索返回来源证据，尚未接入下一阶段的课堂生成式问答。
