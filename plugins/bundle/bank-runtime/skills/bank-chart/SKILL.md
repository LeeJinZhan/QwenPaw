---
name: bank-chart
description: Use when 用户要求生成可在网页编辑的关系图、股权图、组织图、系统架构图、流程图、基础泳道或时序图、统计图。
metadata:
  builtin_skill_version: "1.0"
---

# 图表与关系可视化

从现有对话描述、已上传文件、已授权工具结果整理图表。没有独立接入页或空白画布入口。Skill 不授予工具、数据或网络权限，只调用当前实际可用的 `chart_generate`；不得改用脚本、shell、任意 URL 或静态图片冒充可编辑成果。

1. 复用当前范围和已取得材料。只有关键关系、企业身份、单位等歧义会改变含义时才澄清。演示数据明确为示例；业务数据不编造。附件使用现有读取链路，统计要求先确认全量读取或有效聚合，不以预览行代表全表。
2. 提交 `definition`：`schema_version="chart/1"`、`kind`、`title` 和类型数据。所有对象 `id` 唯一且稳定；不传坐标、身份、HTML、脚本、路径或 URL。
3. 关系/流程：`nodes=[{id,label,shape}]`、`edges=[{id,source_id,target_id,label}]`。shape 为 rectangle/rounded/ellipse/diamond/text；节点可有 entity_type、business_id、description、parent_id。分组 `groups=[{id,label,parent_id?}]`，容器不得循环。股权关系可有 `ratio`（0–1 十进制字符串，未知为 null）；标签与比例一致，不均分或补零。目标企业上下各三层，稳定标识去重，保留循环、多股东及已取得范围。
4. 泳道：`kind="swimlane"`，在节点/边之外增加 `lanes=[{id,label,order}]`，每节点有 lane_id。时序：`kind="sequence"`，使用 `participants=[{id,label,order}]` 与 `messages=[{id,from_id,to_id,order,label,message_type}]`；类型 request/return，排序唯一；不混入 nodes/edges 或完整 UML 片段。
5. 统计：`kind="statistical"`、chart_type（column/bar/line/area/pie/donut）、categories、`series=[{id,name,values}]`，可带 unit/legend/axis_labels。values 为十进制字符串或 null，长度与分类一致；缺失值不默认填零。饼/环限单系列、非负、合计大于零。精度保留原字符串。
6. `source_refs` 使用真实授权的 `{kind,ref_id,complete,location?,note?}`，kind 为 file/tool_result/conversation。file 可带 file_scope=session_file/workspace_file/generated_file，省略时为 session_file。纯描述可为空，Runtime 注入当前会话；材料来源不得伪造或省略。来源不完整先说明范围，不虚构全量。
7. 工具返回 chart_status=ready 且有 chart_id/version_id 才能确认生成。accepted/queued/未知结果不算完成，不反复创建。字段错误按诊断修正一次；权限拒绝不能绕过。

用户在卡片打开编辑器后可手动编辑；显式保存产生新版本，不增加对话轮次。“保存到工作区”复制完整图表。不要承诺画布选区对话修改、Visio 回传或多人协作。

VSDX 提示：导出后可在 Visio 2016 中编辑，请注意样式和布局可能与当前展示不同。

有效的直接描述示例：
```json
{"definition":{"schema_version":"chart/1","kind":"flow","title":"示例审批流程","nodes":[{"id":"apply","label":"提交申请","shape":"rounded"},{"id":"review","label":"审核","shape":"diamond"}],"edges":[{"id":"submit","source_id":"apply","target_id":"review","label":"提交"}]},"source_refs":[]}
```

答复前核对：数据是否真实或明确为示例？引用是否确实取得？结果是否实际 ready？不得用文字承诺替代真实卡片。
