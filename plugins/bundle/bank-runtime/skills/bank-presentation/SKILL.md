---
name: bank-presentation
description: 用于制作、修订 PPT/PPTX 演示文稿，包含经营汇报、普惠金融、客户方案、培训、科技及文化介绍；按内容组织可编辑图表、图文和八套主题。
---
# 银行演示文稿

技能不授予工具、文件、MCP 或网络权限。仅使用当前真实可用且已授权的成果工具和材料。不要使用 shell、脚本、路径或外部 URL 生成 PPT；使用 `artifact_generate`，修改已有成果使用 `artifact_revise`。本文件包含完整规则，不读取插件目录。

## 内容与主题

先确定受众、用途、页数和每页主要观点；用户要求 N 页时总计 N 页，不额外添加封面。数据只来自用户材料或实际检索结果，标明单位与来源；示例数据必须显式标记。需要行内制度依据时先按 bank-document-qa 查证；纯排版不额外检索。不因为做 PPT 而调用公文 DOCX 结构。

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
