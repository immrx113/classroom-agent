# 05 课件、索引与检索

[返回总览](README.md)

## 现状与问题

`knowledge_service.py` 主要索引纪要和上传文本；原文没有完整自动索引链路。`document_parser.py` 合并 PDF 页面导致页码丢失，DOCX 主要提取段落；还把 `.doc` 路由给 DOCX 解析，不能视为可靠的旧 Word 支持。现有删除按标题关联分块，缺少稳定文件身份。Rerank 是规则评分，先截取再重排，中文检索效果未验证。

修改文件：`document_parser.py`、`document_chunker.py`、`knowledge_service.py`、`embedding_service.py`、知识库模型和 API。

## 文档解析

- Parser 输出 `ParsedBlock[]`，每块包括 block_id、text、heading、page_no 或 paragraph_index、表格位置和来源版本。
- PDF 保留逐页定位；DOCX 提取段落、表格和标题层级，使用段落/表格定位，不伪造固定页码。
- 空文本 PDF 返回“需要 OCR”，不把空索引算成功；PPTX/OCR 作为后续独立 provider。
- `.doc` 首版明确拒绝并提示转换，除非后续加入并验证专用转换链路。
- 文件按 document_id/version 删除，名称相同不代表相同文件。

## 索引设计

| 检索来源 | 内容 | 必须保留的定位 |
| --- | --- | --- |
| 课堂原文 | 完整有效转写分块 | transcript_version、segment 范围、起止时间 |
| 课件原文 | PDF/DOCX/TXT/MD 解析块 | document_version、block_id、页码或段落 |
| 知识点/例题 | 已发布结构化产物 | artifact_id、note_version、evidence_ids |
| 课堂笔记 | 章节摘要 | note_version、section_id、原始证据关联 |

课程、课时、来源版本、活动状态使用可索引字段，不只放入不受约束的 JSON。Embedding 记录模型、维度和内容摘要；维度变化采用新索引版本重建，禁止混写不可比较向量。

建立向量与全文索引前验证数据规模、查询计划和中文效果；现有数据库选择继续保留，不预先承诺某种索引必然达到指定延迟。

## 检索流程

1. 根据 ScopeContext 限定 owner、course、可选 lecture、有效来源版本与演示状态。
2. 对原文和知识点分别构造候选；向量与词法检索均在过滤后执行。
3. 候选合并后做 RRF、去重和重排，再截取最终上下文；规则重排先保留为基线。
4. 以源片段或文档块 ID 识别同源重复，中文短文本不只依赖字符相似度去重。
5. 对例题、定义条件和推导，按预算补取相邻片段；控制单一来源挤占上下文。
6. 返回统一 evidence 对象与检索轨迹，供问答引用与诊断。

并发召回时每条数据库分支使用独立 AsyncSession；不能把同一个异步 ORM session 直接并发使用。缓存若引入，键需包含主体、范围、查询和索引版本。

## 版本发布与故障

索引构建使用独立 version 状态：building → ready → active。构建失败不替换当前可用索引；若转写已更新但索引仍旧，前端与问答显式标记索引更新中，不能静默混用不同版本。删除课程或撤销权限立即过滤，物理清理异步执行。

检索不可用与没有相关内容分别返回 `retrieval_error` 和 `no_evidence`。向量失败可以降级词法，但不得去掉课程或权限过滤；降级状态进入日志和回答元数据。

## 接口与验收

目标接口：`POST /courses/{id}/documents`、`GET /courses/{id}/documents`、`DELETE /documents/{id}`、`POST /knowledge/search`、`GET /lectures/{id}/index-status`。

固定标注问题集覆盖中文术语、同义表达、同名概念跨课程、末尾片段、例题和无答案问题。比较向量、词法、融合、重排的 Recall@K/MRR、来源覆盖与延迟；参数与模型选择依据结果决定。验证同名文件删除互不影响、旧版本不参与新检索、每个召回结果都能打开实际来源。

依赖：模块 01/03/04/07；下游：模块 06/08/09。
