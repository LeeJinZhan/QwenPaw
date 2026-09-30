---
name: bank-chart
description: Use when 用户要求生成可在网页编辑的关系图、股权图、组织图、系统架构图、流程图、基础泳道或时序图、统计图。
metadata:
  builtin_skill_version: "1.4"
---

# 图表与关系可视化

从现有对话描述、已上传文件、已授权工具结果整理图表。没有独立接入页或空白画布入口。Skill 不授予工具、数据或网络权限，生成调用当前实际可用的 `chart_generate`，导出现有图表调用 `chart_export`；不得改用脚本、shell、任意 URL 或静态图片冒充可编辑成果。

1. 复用当前范围和已取得材料。只有关键关系、企业身份、单位等歧义会改变含义时才澄清。演示数据明确为示例；业务数据不编造。附件使用现有读取链路，统计要求先确认全量读取或有效聚合，不以预览行代表全表。
2. 提交 `definition`：`schema_version="chart/1"`、`kind`、`title` 和类型数据。所有对象 `id` 唯一且稳定；不传坐标、身份、HTML、脚本、路径或 URL。
3. 关系/流程：`nodes=[{id,label,shape}]`、`edges=[{id,source_id,target_id,label?}]`。连线 label 可省略；没有文字时不传该字段，不传空字符串。判断分支保留“是/否”等实际标签。shape 为 rectangle/rounded/ellipse/diamond/text；节点可有 entity_type、business_id、description、parent_id。分组 `groups=[{id,label,parent_id?}]`，容器不得循环。股权关系可有 `ratio`（0–1 十进制字符串，未知为 null）；标签与比例一致，不均分或补零。目标企业上下各三层，稳定标识去重，保留循环、多股东及已取得范围。
4. 泳道：`kind="swimlane"`，在节点/边之外增加 `lanes=[{id,label,order}]`，每节点有 lane_id。时序：`kind="sequence"`，使用 `participants=[{id,label,order}]` 与 `messages=[{id,from_id,to_id,order,label,message_type}]`；类型 request/return，排序唯一；不混入 nodes/edges 或完整 UML 片段。
5. 统计：`kind="statistical"`、chart_type（column/bar/line/area/pie/donut）、categories、`series=[{id,name,values}]`。可选 unit 为字符串；legend 和 axis_labels 都是布尔开关 true/false，不是轴标题对象或数组。values 为十进制字符串或 null，长度与分类一致；缺失值不默认填零，缺失处断开使用 `missing_values="gap"`。饼/环限单系列、非负、合计大于零。精度保留原字符串。
6. `source_refs` 使用真实授权的 `{kind,ref_id,complete,location?,note?}`，kind 为 file/tool_result/conversation。file 可带 file_scope=session_file/workspace_file/generated_file，省略时为 session_file。纯描述可为空，Runtime 注入当前会话；材料来源不得伪造或省略。来源不完整先说明范围，不虚构全量。
7. 工具返回 chart_status=ready 且有 chart_id/version_id 才能确认生成。accepted/queued/未知结果不算完成，不反复创建。字段错误按诊断修正一次；权限拒绝不能绕过。

用户在卡片打开编辑器后可手动编辑；显式保存产生新版本，不增加对话轮次。“保存到工作区”复制完整图表。不要承诺画布选区对话修改、Visio 回传或多人协作。

导出现有图表时，使用已取得的真实 chart_id 和指定 format 调用 `chart_export`；导出当前图表时省略 version_id，由 Runtime 选择并冻结最新保存版本，只有用户要求某个已知历史版本时才传真实 version_id。保留该保存版本的数据、样式和布局，不把图表重新拼成 `artifact_generate` 的静态图形。无法确认图表标识时说明缺少的信息，不编造标识。用户明确指定 PNG 时必须交付 PNG；导出失败不能擅自改为 SVG、PDF 或其他格式。超时、资源不足属于执行失败，不是数值类型错误，不反复修改数字/字符串类型重试。只有返回 succeeded 和真实 generated_file_ids 才能说明已导出；失败后说明指定格式尚未交付，其他格式成功不能覆盖原失败。

VSDX 提示：导出后可在 Visio 2016 中编辑，请注意样式和布局可能与当前展示不同。

## 首次调用前检查

先选定一种 kind，再按对应结构一次构造完整的工具参数对象。直接调用 `chart_generate`，不要把 JSON 或 Markdown 代码块当作最终成果。下面是格式示例，不替代用户的数据和要求。

- 每个对象的 id 使用英文字母开头，例如 n_start、e_back、lane_dev、series_online；中文业务名称写入 label 或 name。引用字段必须精确匹配已定义的 id，不能直接使用显示名称。
- 流程图先列齐所有步骤、判断节点与返回连线；判断用 diamond，按用户要求标注分支。无文字连线省略 label，不能传 `""`。不得为避免循环省略业务返回线。
- 泳道先定义 lanes（id、label、从 0 开始不重复的整数 order），再为每个节点填有效 lane_id；跨泳道仍用 source_id/target_id 连接节点，不把泳道 id 当作节点 id。
- 时序只传 participants/messages；消息使用 from_id/to_id，不能套用流程图的 source_id/target_id。参与者和消息各自的 order 不重复，message_type 只能是 request 或 return。
- 统计的每个系列必须同时有 id、name、values。逐项对齐 categories，全部数值写成十进制字符串；缺失写 JSON null，不能写“缺失”、空字符串、NaN 或补零。断开缺失点用 missing_values="gap"。legend/axis_labels 只传 true/false，不传配置对象。
- 最后核对用户要求的标题、单位、系列数量、分支和缺失位置；只保留当前 kind 允许的字段。仅在工具返回字段诊断后修正对应字段一次，不重复生成已 ready 的成果。
- 用户未指定配色、字号或线条样式时，省略 style、relation_type 等可选装饰字段，采用编辑器默认样式；不要为每个节点重复添加样式。完成必需数据后直接调用工具，ready 后简洁说明成果，不在回复里重复整份 JSON。

有效的直接描述示例：
```json
{"definition":{"schema_version":"chart/1","kind":"flow","title":"示例审批流程","nodes":[{"id":"apply","label":"提交申请","shape":"rounded"},{"id":"review","label":"审核","shape":"diamond"}],"edges":[{"id":"submit","source_id":"apply","target_id":"review"},{"id":"back","source_id":"review","target_id":"apply","label":"否"}]},"source_refs":[]}
```

含缺失值的示例折线图（示例数据）：
```json
{"definition":{"schema_version":"chart/1","kind":"statistical","title":"示例业务量","chart_type":"line","categories":["1月","2月","3月"],"series":[{"id":"online","name":"线上业务","values":["120",null,"180"]},{"id":"branch","name":"网点业务","values":["100","110","130"]}],"unit":"笔","legend":true,"axis_labels":true,"missing_values":"gap"},"source_refs":[]}
```

含跨泳道返回线的示例：
```json
{"definition":{"schema_version":"chart/1","kind":"swimlane","title":"示例交付流程","lanes":[{"id":"lane_dev","label":"研发方","order":0},{"id":"lane_test","label":"测试方","order":1}],"nodes":[{"id":"n_dev","label":"开发与修复","shape":"rounded","lane_id":"lane_dev"},{"id":"n_test","label":"测试是否通过","shape":"diamond","lane_id":"lane_test"},{"id":"n_end","label":"结束","shape":"ellipse","lane_id":"lane_test"}],"edges":[{"id":"e_test","source_id":"n_dev","target_id":"n_test"},{"id":"e_back","source_id":"n_test","target_id":"n_dev","label":"否"},{"id":"e_end","source_id":"n_test","target_id":"n_end","label":"是"}]},"source_refs":[]}
```

含请求与返回的示例时序图：
```json
{"definition":{"schema_version":"chart/1","kind":"sequence","title":"示例查询时序","participants":[{"id":"p_user","label":"业务人员","order":0},{"id":"p_system","label":"业务系统","order":1}],"messages":[{"id":"m_query","from_id":"p_user","to_id":"p_system","order":0,"label":"查询","message_type":"request"},{"id":"m_result","from_id":"p_system","to_id":"p_user","order":1,"label":"返回结果","message_type":"return"}]},"source_refs":[]}
```

答复前核对：数据是否真实或明确为示例？引用是否确实取得？结果是否实际 ready？不得用文字承诺替代真实卡片。
