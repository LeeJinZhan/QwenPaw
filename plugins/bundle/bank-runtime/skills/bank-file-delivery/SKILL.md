---
name: bank-file-delivery
description: 已确定需要生成、修订或转换 Word、表格、PDF、静态页面或固定图形文件时，提供成果工具参数与格式规则。纯文字问答、写作和内部读取转换无需加载。
---
# 文件交付

仅在当前或仍有效的历史要求包含文件交付时使用本技能。沿用已确认的内容、格式和来源；正文写作方法见 `bank-document-writing`，PPT 与可编辑图表分别使用 `bank-presentation`、`bank-chart`。

Skill 不授予权限；身份、来源和工具范围由可信 Runtime 上下文确定。不得改用 shell、脚本、任意 URL 或路径绕过拒绝。以下示例只说明参数结构，不是用户事实或新增交付要求。

## 通用格式与操作契约

- 新建已要求的静态文件使用 artifact_generate；修订已生成成果用 artifact_revise，传真实 source_generated_file_id、instructions、完整 content 和可选 output_name。上传附件先读取再生成；不把上传编号用作已生成成果编号。
- DOCX 的文种、版式与 delivery_plan 见本技能下文；正文起草方法由 bank-document-writing 提供，PPTX 的主题和 slides 由 bank-presentation 提供，不在通用入口重复加载其全部参数。机构模板仅使用已发布且当前授权的 template_version_id 调用 template_fill_docx。
- XLSX 使用 sheets 数组，每项 name 与 rows 二维数组；headers 可单列提供。所有集合直接传数组，无 item 包装；content 对象不再次序列化成字符串。
- 可修改公式写成 {"formula":"=SUM(B2:B5)"}，仅引用本工作簿已提供范围，支持算术比较和 SUM/AVERAGE/MIN/MAX/COUNT/ROUND/ABS/IF/IFERROR/SUMIF/COUNTIF，可跨表引用。普通等号开头字符串按文本处理；不提供缓存值、外链、宏或未登记函数。生成器重算后才发布，宽表分页不删内容，极长单元格仍受 Excel 容量约束。
- HTML 是静态阅读页面，允许页面外壳、正文、表格和静态样式，不支持脚本、事件属性、交互表单或外链资源。校验失败按真实诊断和预算修正。
- PNG/JPEG/WEBP/SVG 仅支持确定性的 chart、table、flowchart、cover。chart 字段为 kind、chart_type、title、categories、series 和可选 style_profile；商务风可用 executive。示例结构：`{"kind":"chart","chart_type":"bar","title":"示例","categories":["一月"],"series":[{"name":"数量","values":[1]}]}`。不猜坐标或样式字段，不传自然语言图片提示词、SVG 源码、URL 或路径；cover 仅用于目标需要的标题图。
- 仅明确要求交付 PDF 时使用 artifact_type=pdf 和 explicit_pdf_request=true。直接 PDF 可使用 sections/paragraphs 或受支持 Markdown 表格；Office 转 PDF 保留原版式。
- 格式转换使用 artifact_convert。上传来源传 source_type=session_file/workspace_file 与 source_id；已生成成果传 source_generated_file_id，两类不能混用。只选当前工具支持的格式，不把重新生成冒充转换。
- 下载转换用 purpose=delivery；读取所需内部转换用 purpose=read，内部 PDF 无需虚构 explicit_pdf_request，也不主动发布为下载成果。旧版 Office 或图形的安全转换与覆盖范围按 bank-document-qa 核对，转换成功不等于已经读取。

## 内部工具参数示例


以下是内部结构示例，不是要求用户填写的表单。文件编号含 example_only 的值仅为占位，调用必须替换为实际获授权的对应来源；示例数据不用于用户的真实统计。无来源时不猜编号；不在用户回复中展示这些参数。

生成 Excel：金额为数值，表头只出现一次，不把汇总示例当真实数据。

```json
{"artifact_type":"xlsx","title":"汇总结构示例","output_name":"汇总结构示例.xlsx","content":{"sheets":[{"name":"结构示例","headers":["网点","金额（万元）"],"rows":[["示例网点A",100],["示例网点B",200],["示例合计",300]]}]}}
```

仅在用户已明确要求 PDF 时生成；普通 Word 请求不能因失败而改成 PDF。PDF 不携带 DOCX 专用的 delivery_plan。普通报告优先使用 sections/paragraphs，标题单独传 title，不在正文中重复顶层标题；复杂表格需使用当前已支持的排版能力，不假定旧版 PDF 渲染器会解析 Markdown。

```json
{"artifact_type":"pdf","title":"说明示例","output_name":"说明示例.pdf","content":{"sections":[{"heading":"测试目标","paragraphs":["检查文件内容是否完整，所有数据均为示例。"]},{"heading":"结论","paragraphs":["示例测试记录已汇总，待按实际结果核对。"]}]},"explicit_pdf_request":true}
```

将上传的旧版 Word 转为可读取的 DOCX。转换成功后使用实际返回的附件引用读取；若未返回可读引用，只说明转换已完成、读取未完成，不猜文件路径或配对引用，也不反复转换试探文件名。

```json
{"source_type":"session_file","source_id":"uploaded_doc_example_only","target_format":"docx","purpose":"read","output_name":"转换示例.docx"}
```

将上传的旧版 Excel 转为可读取的 XLSX。

```json
{"source_type":"session_file","source_id":"uploaded_xls_example_only","target_format":"xlsx","purpose":"read","output_name":"转换示例.xlsx"}
```

用户明确要求将已生成的 Word 转为 PDF。

```json
{"source_generated_file_id":"generated_example_only","target_format":"pdf","output_name":"转换示例.pdf","explicit_pdf_request":true}
```


## 已确定 DOCX 交付时的文种与版式

沿用已确定的写作与文件目标。修改或转换前取得真实来源；从零起草不机械索要附件。多成果按实际结果逐项核对，内部读取转换与用户下载交付分开。

按沟通目的判断，不机械匹配词语：

| 用户用途 | document_type | 默认 layout_kind |
| --- | --- | --- |
| 向另一单位商洽、询问或答复的函 | letter | official_document |
| 向有审批权的上级请求批准的请示 | request | official_document |
| 向部门下发、要求落实的任务/会议通知 | notice | official_document |
| 向上级正式报送工作情况的报告 | report | official_document |
| 数字金融研究报告、一般分析文章 | report / article | standard_document |
| 个人待办、项目任务清单 | task_list | standard_document |
| 工作方案 | work_plan | 结合是否正式行文判断 |
| 介绍公文写作、解释“函”的含义、培训示例 | article | standard_document |

用户明确要普通 Word、不要公文排版时，遵从 standard_document；明确要公文版式时遵从 official_document。文种与版式不是同一个字段，例如用户要求普通排版的函仍可 document_type=letter。只说“任务”且上下文无法区分个人清单和正式下发通知时，简短问清用途；已说明主送部门和执行安排时直接起草。缺失事实不凭空补全。

DOCX 的 artifact_generate / artifact_revise 调用必须同时提交严格三字段 delivery_plan，例如 `{"document_type":"notice","target_format":"docx","layout_kind":"official_document"}`；下文给出与正文一起提交的完整请求。

document_type 仅支持 letter、request、notice、report、work_plan、task_list、article、other；layout_kind 仅支持 official_document、standard_document。这三个字段是工具参数，不作为正文或内部推理展示。公文 content 必须使用下文固定版式，普通文档可用完整正文字符串或 sections/paragraphs 对象；对象不再次序列化为 JSON 字符串。重试保持已确认版式。修订同样提交完整判断与完整正文，改变版式需要用户意图支持。

模板优先级高于上述默认版式：用户明确选择机构模板时走 template_fill_docx，使用真实已发布且当前授权的 template_version_id；不虚构版本、不自动选择未知模板。固定公文版式的 layout_version 为 bank-official-docx-v1，模板版本为空；不能将它冒充机构模板。

## 普通 Word 与来源选择

“生成 Word”沿用刚确认的正文和用途，不重新问标题或文种，不先返回大纲等待确认。内容足够时直接生成。用户上传的文件不是已生成成果：上传原稿需实际读取后用 artifact_generate 生成新稿；artifact_revise 只接受工具真实返回的 source_generated_file_id。只改局部时保留其余全文，不把修订建议或摘要当作完整正文。

普通 Word 完整参数示例（示例正文仅用于说明结构，实际用当前确认稿替换）：

```json
{"artifact_type":"docx","title":"工作说明示例","content":{"sections":[{"heading":"工作安排","heading_level":1,"paragraphs":["测试完成后，各组汇总结果并提交测试记录。"]}]},"output_name":"工作说明示例.docx","delivery_plan":{"document_type":"article","target_format":"docx","layout_kind":"standard_document"}}
```

普通 sections 可用 heading_level（1–6 的整数，默认 1）表达标题层级。tables 使用等列二维数组，首行为表头；普通 Word 宽表自动横向分节，过宽时分组并重复首列，长行允许跨页、表头重复。不要为容纳表格删列或把正文缩成摘要。

公文保持固定纵向纸张及公文字体；宽表通过分组、重复首列处理，输入预算为 32 列、1000 数据行，仍受全文 100000 字符预算约束。

直接 PDF 的 Markdown 表格支持按内容计算列宽、纵横混排、分组与重复表头；Office 导出 PDF 保留原文件版式。

## 公文 DOCX 交付

普通文字与公文写作由当前助手直接完成，不因为文种委派专家。用户只要正文时直接交付正文。要求公文 DOCX 时调用当前获授权的 `artifact_generate`；以下是完整参数，content 直接提交对象，kind 和 layout_version 位于 content 内、document 外：

```json
{"artifact_type":"docx","title":"工作安排通知初稿示例","output_name":"工作安排通知初稿示例.docx","delivery_plan":{"document_type":"notice","target_format":"docx","layout_kind":"official_document"},"content":{"kind":"official_document","layout_version":"bank-official-docx-v1","document":{"title":"关于工作安排的通知","recipients":["各测试小组"],"blocks":[{"type":"heading","level":1,"text":"一、工作安排"},{"type":"paragraph","text":"测试完成后，各组汇总结果并提交测试记录。"}]}}}
```

此为字段示例，不是用户事实。document 可选字段：signatory、date、classification、urgency 为字符串，attachments、cc 为字符串数组。blocks 支持 paragraph（text）、heading（level 为 1–4 的整数、text）与 table（headers 为非空字符串数组、rows 为等宽字符串二维数组）。正文每个语义段落一个块，不合并丢段；数组直接传数组，不加 item、不转成 JSON 字符串或代码块。所有主送和抄送完整保留；recipients 每项填写一个单位名称，不自行添加冒号、换行或放进正文 blocks，主送段落由渲染器排版。布局由服务端固定，不传字体、路径、脚本、外链、命令或身份。

无明确事实不补日期、密级、文号、署名、附件或抄送。可以交付初稿时说明缺项，关键缺项影响实质内容时集中澄清。以“公文初稿”命名交付，不假冒签发、审批或正式定密。固定版式不是已发布机构模板。

在已确定的公文 DOCX 任务中，用户要求“直接生成初稿”时，沿用前文主题和文种；不影响起草的缺失事实用明确待补标记，例如【待填写】或【待核实】，不机械要求全部补齐，不编造事实。关键缺项影响实质内容时集中澄清。按实际文种选择 document_type，使用 official_document 版式与公文 content 对象。若工具提示 JSON 不完整，应重新提交完整 kind/layout_version/document 对象，不将公文改成普通 sections/paragraphs，也不把对象再次编码成字符串。

主送、落款单位和成文日期必须使用专用字段：document.recipients、document.signatory、document.date，不能放入 blocks 普通段落。主送由排版器顶格排在标题后，落款与日期由排版器在正文后右对齐；不要靠空格、换行或正文缩进模拟。初稿缺项使用能辨识含义的待补标记，用户已指定占位方式时遵从；不擅自将当前日期或登录人的部门当成成文日期、申请单位。blocks只放正文标题、正文段落与表格。

用户明确指定机构模板时，仅用已获授权的真实已发布版本调用 `template_fill_docx`，使用实际字段 schema。模板未发布、停用或无权时说明限制，不以固定版式规避；用户未指定机构模板时才使用上述固定版式。

后续修改使用 `artifact_revise`，携带已有受控文件引用及完整修订结构。外层文件名可变化，`document.title` 保持用户确认的正文标题；不得覆盖旧文件。要求普通 Word 而非公文时使用完整正文字符串或普通 DOCX sections/paragraphs 对象。

生成成功且实际返回当前文件引用后，才说明文件已生成，由原文件卡片和工作区交付。缺字体、未知版式或校验失败时保留可用正文，准确说明尚未交付文件，不静默降级或改用 shell、write_file 等绕过。结构输入错误可据工具给出的安全提示修正后通过同一工具重试；权限拒绝不得规避。已发布文件和后续回答失败分别说明。


修订完整参数示例：`generated_example_only` 仅为结构占位，调用前必须替换为本轮可用且已授权的真实成果引用；没有该引用时不试填编号。content 包含未修改的全部段落；不添加 artifact_type、title 或 source_refs 等修订工具不接受的外层字段。

```json
{"source_generated_file_id":"generated_example_only","instructions":"主送增加运维小组，新增问题反馈要求，保留其余内容","output_name":"工作安排通知修订初稿示例.docx","delivery_plan":{"document_type":"notice","target_format":"docx","layout_kind":"official_document"},"content":{"kind":"official_document","layout_version":"bank-official-docx-v1","document":{"title":"关于工作安排的通知","recipients":["各测试小组","运维小组"],"blocks":[{"type":"heading","level":1,"text":"一、工作安排"},{"type":"paragraph","text":"测试完成后，各组汇总结果并提交测试记录。"},{"type":"heading","level":1,"text":"二、问题反馈"},{"type":"paragraph","text":"发现的问题于测试当天统一汇总。"}]}}}
```

## 面向用户的交付

用户只要正文时直接给完整正文；要求文件时，确认本次文件已生成后可答“文档已生成，可在文件卡片中打开或下载。”修订任务按需补充实际修改点，不重复展示整篇内容，除非用户要求。

工具输入示例、字段约定和自检规则留在执行环节，不写入正文、文件或交付说明。文件尚未生成时说明实际阻塞；已有可靠稿件可先给出，并明确它还不是可下载文件。结果未知时说明尚未确认，不使用成功示例；文件已交付但其他要求未完成时分别说明。

## 文件参数失败后的恢复

运行时仅在尚未提交成果操作、无来源文件的普通 Word 参数生成异常时，可触发一次正文恢复。只有收到该恢复指令才采用其短元信息与完整正文格式；正文仍须满足原主题、篇幅和事实要求。程序组装后的请求继续经过原有权限、参数校验和成果发布流程；草稿返回不等于文件已生成，不能降级公文、改用未授权模板或把内部元信息展示给用户。

生成 Word 时把完整正文直接放入 content 对象（paragraphs/sections 或指定公文结构），不要手工拼接或二次 JSON 编码。一次调用必须同时包含正文、文件信息和所需 delivery_plan，不能先调用只有文件名的半成品参数。收到 JSON 不完整或结构校验错误时，重新构造完整对象并正确转义正文引号，按具体诊断和剩余恢复预算修正；不允许恢复或无实质进展时说明未生成，不重复播报准备生成，不换工具绕过校验。
