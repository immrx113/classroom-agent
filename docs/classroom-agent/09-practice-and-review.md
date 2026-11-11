# 09 练习、反馈与复习

[返回总览](README.md)

## 范围与现状

当前没有完整题目、作答、评分、错题和复习模型。本模块放在课堂整理与有证据问答稳定后的 M3，不阻塞录音问答 MVP。先支持按需出题与作答反馈，再考虑自动调度。

新增 `practice_service.py`、`grading_service.py`、`api/practice.py`、题目与作答模型、`frontend/src/features/practice/`。

## 数据设计

| 实体 | 核心字段 |
| --- | --- |
| practice_sets | id、user_id、course_id、lecture_id、source_version、生成配置 |
| questions | id、type、stem、options、reference_answer、rubric、knowledge_point_ids、evidence_ids、status |
| attempts | id、user_id、question_id、question_version、answer、submitted_at、grade_status |
| grading_results | attempt_id、score、max_score、rubric_results、feedback、evidence_ids、grader_version、review_status |
| learning_events | user_id、knowledge_point_id、event_type、attempt_id、结果与时间 |
| review_items | user_id、knowledge_point_id、next_review_at、status、调度算法版本 |

题目内容和评分标准按版本固定。学生接口不返回标准答案和评分细则中的隐藏答案字段；参考答案在提交后按产品规则展示。

## 出题与判分

1. 用户选择范围、题型、数量和难度，从已发布且证据充分的知识点生成。
2. 每题保存来源、参考答案与评分要点；检查条件、选项、答案和解析是否一致。
3. 先支持单选与简答；复杂证明、开放作文和公式等价判定后续增加。
4. 单选用确定性规则评分；简答使用评分要点逐项判断，返回扣分依据和引用。
5. 评分模型输入将学生回答视为数据，不能执行其要求修改评分标准的指令。
6. 评分失败和无法判断使用 needs_review，不判作零分或正确；允许用户反馈和重新判分，保留历史结果。
7. 模型估计的题目难度与实际学习难度分开，后者需依据作答统计。

## 学习状态与调度

M3 首版展示“最近作答结果、错误次数、待复习”，不直接输出未经校准的精确掌握百分比。教师易错点和学生答错记录分别展示。

复习调度后续基于作答结果与用户反馈实现，保存算法版本和事件依据。课程原文纠错导致题目失效时暂停该题及关联调度，重新生成后由明确版本接续。

## 目标接口

| 接口 | 行为 |
| --- | --- |
| POST /practice-sets | 提交出题任务 |
| GET /practice-sets/{id} | 返回题目，按阶段隐藏答案 |
| POST /questions/{id}/attempts | 幂等提交作答，返回评分状态 |
| GET /attempts/{id} | 查看反馈与证据 |
| POST /attempts/{id}/feedback | 用户指出判分问题 |
| GET /review-items | 获取待复习项目 |

## 验收

- 无证据内容不能生成“课堂原题”；模型生成题明确标记为生成练习。
- 同一提交 request_id 不会产生重复作答或重复计分。
- 用固定题目和人工评分样本检查简答判分一致性，不以结构化 JSON 成功率代替判分准确性。
- 学习状态变化可追溯到作答或反馈；仅上传录音不会自动提高掌握状态。
- 多用户错题与评分记录严格隔离，题目更新不改写旧作答依据。

依赖：模块 01/04/05/06/07/08。
