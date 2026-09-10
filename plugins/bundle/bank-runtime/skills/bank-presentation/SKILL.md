---
name: bank-presentation
description: 用于制作、修订 PPT/PPTX 演示文稿，包含经营汇报、普惠金融、客户方案、培训、科技及文化介绍；按内容组织可编辑图表、图文和八套主题。
---
# 银行演示文稿

技能不授予工具、文件、MCP 或网络权限。仅使用当前真实可用且已授权的成果工具和材料。不要使用 shell、脚本、路径或外部 URL 生成 PPT；使用 `artifact_generate`，修改已有成果使用 `artifact_revise`。本文件包含完整规则，不读取插件目录。

## 交互与执行节奏

沿用已确认的要求和有效材料，不要重复询问或让用户重新粘贴。只有关键缺项才集中询问；一般措辞、结构和样式合理选择，已有授权仍有效，权限与审批限制仍需遵守。

本文件的规则、参数表和检查项是执行指导，不是用户正文或报告素材。内部完成技能读取与结果核对，不播报字段校验、工具 JSON 或反复“检查环境”。简单任务直接交付，耗时任务只简短说明实际进展或阻塞；用户询问技术原因时解释已核实的信息。

同一确定参数错误最多修正重试一次；结果未知时不重复提交，只用已有授权能力核对状态。无法核对时说明尚未确认，不让用户填写内部字段，也不以换工具绕过权限、配额或连接故障。仅按已确认结果说明完成范围，后续成功恢复的步骤不继续描述为当前失败。

## 执行顺序与工具选择

先复用已经实际读取且仍适用的材料，只补读本次缺少的部分，再在内部规划完整页序、主题与版式并检查参数。首次生成只提交一次完整请求；后续修正遵守失败处理规则。无法读到必要原稿时说明缺少的材料，不把空白或占位稿当作美化结果。不要为试探参数先生成一份无关文件或一页测试稿。

- 从文字、已解析文档或用户上传的 PPT 重新排版：使用 `artifact_generate`，`artifact_type` 固定为小写 `pptx`，提供 title 和完整 content。上传 PPT 的文件编号不是 `source_generated_file_id`；上传原稿须先按已授权读取能力理解，再生成新稿，不能仅凭文件名声称保留了内容。
- 修订本平台已成功生成的 PPT：使用 `artifact_revise`，仅传工具实际返回的 `source_generated_file_id`、instructions、完整 content 和可选 output_name；不要追加 artifact_type、title、source_refs 等未登记外层字段。完整 content 包含未改动的页面和事实，不是差异补丁。
- PPT 请求不传 DOCX 专用的 `delivery_plan`、kind、layout_version、document，也不传自造的 template/theme_id/style/font/position 等字段。主题放在 `content.theme`。
- 未使用的可选字段直接省略，不用 null、空对象或占位编号补齐。content 为 JSON 对象，slides/items/bullets 为真正数组，不转成字符串，不使用单引号伪 JSON 或 `{"item": ...}` 包装。output_name 如填写，只用无路径的文件名并以 `.pptx` 结尾。
- source_refs 只引用实际已获授权材料，结构为 source_type、source_id、可选 purpose；不抄造编号。未引用文件时省略。图片 source_index 必须指向这次 source_refs 中的真实图片，不能指向原 PPT 本身。

## 内容与主题

从当前内容和要求识别受众、用途、页数与每页观点，不把这些项目当作必填问卷。用户未指定页数且材料足够时，由内容决定合理页数；未指定主题按下表选择，不要求用户在八主题间做选择。用户要求 N 页时总计 N 页，不额外添加封面。数据只来自用户材料或实际检索结果，标明单位与来源；示例数据必须显式标记。需要行内制度依据时先按 bank-document-qa 查证；纯排版不额外检索。不因为做 PPT 而调用公文 DOCX 结构。

用户要求“美化、按行内风格重排”时直接处理原稿，保留事实、必要页面和明确的页数；不先生成大纲或测试页等待确认，除非用户要求先审方案。

用户指定主题优先，否则按用途选择下表；用途不明默认稳健商务。整套主题一致，按内容改变布局，不把每页都排成文字列表。图表、时间轴、流程、图文、对比和卡片按信息关系选择；不为装饰编造图表、图片或数据。

| theme | 中文名 | 场景 |
| --- | --- | --- |
| steady_business | 稳健商务 | 行长办公会、年度总结、战略 |
| modern_operations | 现代经营 | 经营分析、网点对标、业务复盘 |
| inclusive_local | 普惠乡土 | 涉农、乡村振兴、普惠案例 |
| customer_value | 客户价值 | 小微、园区、客户服务方案 |
| wealth_elegance | 财富雅致 | 财富沙龙、客户活动、产品知识 |
| digital_technology | 数字科技 | 科技建设、AI、数字化转型 |
| clear_classroom | 清晰课堂 | 员工培训、制度宣讲、操作指引 |
| red_culture | 红色文化 | 党建、企业文化、地方文化 |

## 工具内容结构

`artifact_type="pptx"`，content 直接传对象，不转 JSON 字符串：

```json
{"theme":"steady_business","brand_name":"用户提供的机构名称","slides":[{"layout":"cover","title":"季度经营分析","subtitle":"用户提供的期间"},{"layout":"cards","title":"重点工作","items":[{"title":"客户服务","description":"用户提供的具体安排","icon":"people"},{"title":"风险管理","description":"用户提供的具体安排","icon":"shield"}]}]}
```

示例中的机构和期间必须替换为真实信息，未提供则省略。仅支持 `theme`、可选 `image_policy`、可选 `brand_name`（最多 36 字）和 `slides`（1–100 页）。每页可用 title（44 字以内）、subtitle（100 字以内）、eyebrow（30 字以内）、source（150 字以内）、conclusion（100 字以内）、speaker_notes（20000 字以内）、tone（light/dark）；subtitle 用于封面、章节、结束、图文或无条目的正文页；section 的 subtitle 与 items 二选一。conclusion 用于正文/数据页，不与封面、章节、结束或图片页混用。同组字段别名只填一个。长解释放讲稿。渲染器会检查容量，出现溢出错误时缩短表述或拆页，保留所有必要事实与用户要求的总页数；无法兼顾时明确说明，不偷偷截断。

| layout | 内容字段与建议 |
| --- | --- |
| cover / title | title、subtitle；可选 image；开场 |
| section / closing | 章节 / 总结；section 最多 3 条短 items |
| agenda | 最多 8 条短 items |
| content | 最多 6 条 bullets，优先 3–4 条；每条简短 |
| cards | 2–6 个 items 对象，title + description；根据主题采用主次分栏、并列模块、步骤或四/六项卡片，正文控制在 25–35 字 |
| metrics | 1–4 个 items，每项 value（字符串，含单位，最多16字）、label 或 title、description；数字必须有依据 |
| timeline / process | 最多 5 个 items，title 表示阶段/年份，description 表示事项；每项建议 20 字以内 |
| comparison | 恰好 2 个 items 对象，title + description |
| chart | chart 对象，可加 conclusion；原生可编辑图表 |
| table | table 对象，最多 5 列、7 行数据；长表拆页 |
| image | image 对象；可选 subtitle 和最多 3 条短 bullets |
| gallery | images 数组，2–4 张图片与说明 |

一个页面只使用一组 items/bullets/paragraphs/content，不重复填多组。items 可为字符串或对象，对象支持 title/name/label、description/desc/content；metrics 另需 value。cards 可用固定 icon：bank、people、leaf、shield、chart、book、none。无需自定义坐标、颜色、字号或图标代码。

### 调用前逐页检查

优先统一使用 layout、title、bullets、items、description 这些规范字段，不同时填别名。以下组合限制比“每页可用字段”更具体，必须逐页遵守：

| 页面 | 必需结构与不可混用项 |
| --- | --- |
| cover/title、closing | title，可选 subtitle；不要放 items/bullets、chart/table 或 conclusion；结束页的总结清单另用 content |
| content | title + 最多 6 条 bullets；有 bullets/items 时禁止 subtitle，副标题意思合入要点或 speaker_notes；没有条目时才可用 subtitle |
| section | subtitle 与最多 3 条 items 二选一；不放 conclusion |
| cards、metrics、timeline/process、comparison、agenda | 用 items；禁止 subtitle；不附 chart/table；cards 的 icon 放在 item 内，不放页面顶层 |
| chart | title + chart，可选 source/conclusion；禁止 subtitle、items/bullets、table；说明放 source、conclusion 或 speaker_notes |
| table | title + table，可选 source/conclusion；禁止 subtitle、items/bullets、chart |
| image | image 引用或自动配图，可用 subtitle 与最多 3 条 bullets；不放 conclusion |
| gallery | images 或自动配图；禁止 subtitle、items/bullets、conclusion |

每条字符串非空且不超过 120 字；item 的 title/name/label 不超过 30 字且只填一个，description/desc/content 不超过 120 字且只填一个。优先将卡片正文控制在 25–35 字。metrics.value 必须是含单位的字符串且不超过 16 字，不传数字；chart.series.values 则必须传数字数组，不把单位或百分号放进数值。chart 的分类名不超过 16 字、序列名不超过 24 字、unit 不超过 20 字；table 表头不超过 24 字、单元格不超过 60 字，均为字符串。

title/subtitle/source 等非正文参数不写换行或制表符；长段落拆成条目，详细解释放 speaker_notes。不得删掉用户要求的事实、限制、来源或页数来换取校验通过。没有真实量化数据就选 content/cards/process，不构造图表或指标。

### 完整调用示例

下例是自足的三页结构示例，不代表用户材料或真实业务数据。使用时按用户原稿替换主题、正文、页序与页数；“行内风格”不等于虚构机构名称或制度。

```json
{"artifact_type":"pptx","title":"AI 辅助编程示例","output_name":"AI辅助编程示例.pptx","content":{"theme":"digital_technology","image_policy":"none","slides":[{"layout":"cover","title":"AI 辅助编程","subtitle":"从需求到验证的协作流程"},{"layout":"content","title":"从清晰需求开始","bullets":["先明确目标与验收条件","将复杂修改拆成可验证步骤","人工核对生成代码与测试结果"],"speaker_notes":"先确认需求和验收条件，再逐项检查代码和测试结果。"},{"layout":"cards","title":"协作与质量检查","items":[{"title":"协作","description":"提供必要上下文，明确任务边界","icon":"people"},{"title":"检查","description":"检查代码、测试结果与访问权限","icon":"shield"}]}]}}
```

图表示例（仅结构说明，实际值取真实材料）：

```json
{"layout":"chart","title":"季度趋势","chart":{"type":"column","unit":"万元","categories":["一季度","二季度"],"series":[{"name":"示例金额","values":[12,18]}]},"source":"示例数据，仅供演示"}
```

chart.type 支持 bar/column/line/donut；1–8 个不同 categories，1–3 个 series，每个 values 与 categories 等长且为有限数值。donut 只允许一个非负且总和大于零的序列。不要填默认“图表标题”、空序列或补造数据。

table 使用 `{"headers":["项目","安排"],"rows":[["用户事项","用户安排"]]}`，单元格为字符串，行列等长；保留单位与限定说明。

## 图片与交付

content.image_policy 支持三个值，省略为 `auto`：

- `auto`：显式 image/images 引用优先。无显式图片时优先本次 source_refs 中未使用的有效图片，再按页面内容匹配离线内置素材。内置素材覆盖家电制造、家具商贸、花卉、物流、餐饮、客户交流、长者服务、数字服务、洽谈空间及岭南水乡/滨水环境。不需要搜索素材文件、不传素材路径或自造素材编号。
- `uploaded_only`：用户要求“只用我上传的图片”、实际网点/客户/项目实拍或证据记录时使用，内置照片不参与。应先按已授权材料明确每张图片用途，并通过 source_index 指定位置；自动回填按来源顺序，不对上传图片做视觉语义识别。没有上传图片则保留无图版式，并说明缺少实拍材料。
- `none`：用户要求“不配照片/纯文字图表”时使用，移除 image/images 引用；该策略仍允许原生图表、流程和图标。

自动匹配只影响无显式图片的封面、image/gallery 及简短 content 页。无图片引用的 image/gallery 可以请求自动选图；无匹配则回退正文，保留页数与文字。普通短正文自动配图有密度限制；图表、表格、指标、复杂正文不改成照片页。内置照片按相关内容匹配，主题仅用于同分选择，每个内置素材一套最多自动使用一次。使用多个图文场景，不把所有页面改成同一图文结构。

用户指定图片应通过 source_refs 正式提供；仅在聊天中提及上传并不会自动纳入生成工具来源。内置照片是预先制作的场景素材，非本行真实网点、客户、企业或项目实拍；不作为事实证据，不显示“AI 生成示意”字样，也不宣称平台具备图片生成模型。没有合适图片时不强配，不承诺固定图片数量。

图片使用 `{"source_index":1,"caption":"真实图片说明"}`；序号对应本次外层 source_refs 数组，从 1 开始。图片必须是当前获授权的 PNG/JPEG/WEBP 文件，每次最多 5 个来源，不传图片路径、URL 或 base64。未指定图片时按下述配图策略处理，不声称已使用用户未提供的实拍素材。截图、证据需保留关键区域；image/gallery 默认按比例适配显示，封面图可裁切。

修订时图片序号沿用原 PPTX 中由本生成器保存的 source_index；用户图片只能复用其中已有图片，不能把原 PPTX 本身当作图片，也不能凭空新增上传图片来源；内置素材可按完整内容重新自动匹配。修订外部上传 PPTX 时，不推测图片序号。

修改主题时显式保留 image_policy、theme、brand_name、事实、数据、来源与讲稿，使用完整新 content 创建版本；不要把变化的配色解读为数据好坏。文件卡片真实返回后才说明已生成。渲染失败保留可用内容并按具体错误修正，不重复相同参数，不绕过受控工具。

## 失败后按证据修正

先判断实际返回属于参数拒绝、渲染失败、权限拒绝还是结果未知。只有明确“未执行”或有确定字段错误时才修正参数；正在执行或结果未知时查询可用状态，不重复创建作业。

- 有字段校验提示：定位提示对应的外层字段或页面，检查该页的版式组合、数组类型、别名、长度和数据维度，修正后重新检查全稿。不要只读错误标题就猜图标或关键词。
- 只返回“信息不完整或格式不正确”：先检查生成/修订的外层字段、PPT 误带 delivery_plan、null 可选值、来源编号和 JSON 类型；再逐页检查上述组合表。book、shield、chart 等已登记 icon 不应凭猜测轮流替换；普通正文中的编程词汇如 for 也不是删改事实的依据。
- 清晰确定的参数错误可作一次有实质修改的重试。没有更具体证据且再次同类失败时，停止盲目重复调用；保留已整理内容，明确文件尚未生成以及需要核对的校验信息。不要继续声称“已生成”或输出猜测的下载链接。
- 容量/溢出错误：缩短页面展示措辞，将必要细节移入讲稿；只有用户允许改变页数时才拆页。不得自动丢页、丢图、丢数据或改成无关文件。权限、配额、连接故障不能靠换布局解决，也不能改用 shell 或未授权工具绕行。

完成标准是本次完整 PPT 成果真实返回且可交付，不是仅生成了文字大纲。

## 面向用户的交付

确认本次 PPT 已生成后可答“演示文稿已生成，可在文件卡片中打开或下载。”按需简述实际保留的内容与版式调整；未核对页数、配图或原稿完整性时不宣称已保留。

结构示例、逐页检查和失败修正规则用于执行，不放进页面、讲稿或聊天正文。尚未生成文件时说明当前未完成项；已有提纲可以给出，但明确它不是 PPT 文件。结果未知时说明尚未确认，不套用成功示例，也不让用户逐项选择内部布局字段。
