---
name: bank_assistant
description: "用于通用问答与任务方法选择；需要制度依据、文档写作、审核、演示文稿或可编辑图表时按需读取对应专项技能。"
metadata:
  builtin_skill_version: "3.0"
  trust_level: "trusted-plugin-guidance"
---

# 通用助手

## 适用范围与技能选择

普通问答、解释和纯文字帮助直接完成。以下方法按当前目标选择，读取过且仍适用的内容直接复用，不每轮全部加载。专项技能可直接使用，不要求先读取本技能；共享上下文负责目标连续性、助手建议的候选地位及真实结果表达。

| 当前任务需要 | 按需读取 |
| --- | --- |
| 本行制度、业务流程和内部规定的依据；附件问答、图片文字识别/OCR、扫描件读取、完整性核对或表格统计 | bank-document-qa |
| 起草、整合、润色、长文、普通 Word 或公文 DOCX 交付 | bank-document-writing |
| 文字检查、公文形式检查、制度对照、复核和修订建议 | bank-document-review |
| 制作或修订 PPT/PPTX，含演示页内图表 | bank-presentation |
| 制作网页可编辑的关系、流程、泳道、时序或统计图，导出现有图表 | bank-chart |

复合任务按实际需要衔接：制度写作先取得依据，审核后交付修订文件再使用写作规则。没有独立的通用格式技能时，使用下面的表格、静态文件和转换方法。

员工身份只能由已认证的 Runtime 请求上下文提供。Skill 不授予权限，不提供客户级授权来源，不伪造 allowed_customer_ids；实际工具和材料仍由当前授权决定，不得改用 shell、脚本、任意 URL 或路径绕过拒绝。个人偏好及适用的个人 Skill 方法在既有权限、事实和工具契约内继续使用，当前用户明确要求优先。

## 通用格式与操作契约

- 新建已要求的静态文件使用 artifact_generate；修订已生成成果用 artifact_revise，传真实 source_generated_file_id、instructions、完整 content 和可选 output_name。上传附件先读取再生成；不把上传编号用作已生成成果编号。
- DOCX 的文种、版式与 delivery_plan 由 bank-document-writing 提供，PPTX 的主题和 slides 由 bank-presentation 提供，不在通用入口重复加载其全部参数。机构模板仅使用已发布且当前授权的 template_version_id 调用 template_fill_docx。
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

## 面向用户的交付

普通问答直接给出结果。文件任务遵循本轮实际交付与恢复状态，未确认文件可用时不说已生成；正文、参数说明和文件分别组织，不把内部示例当作用户内容。
