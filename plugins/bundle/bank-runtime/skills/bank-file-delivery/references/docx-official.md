# 公文 DOCX 交付

适用于已确定 official_document 的 DOCX 交付；先按《Word 类型、来源与模板选择》确定用途、真实来源及模板优先级，公共权限与发布核验遵循技能主文。公文正文方法按需使用 bank-document-writing 的公文写作参考。

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

## 公文表格与纸张

交付前对照确认稿核对文种、正文标题、主送、各正文块、附件、落款、日期和抄送，局部修改保留其余字段及完整正文。生成器校验固定版式与提交内容，不验证事实依据、发文授权或与原稿的语义差异。公文分页应保持标题与后续内容、主送与正文、落款与日期的合理衔接；没有实际渲染检查时，不宣称已经逐页验收。缺项按初稿标记处理，不用补造日期或落款来改善页面外观。

公文保持固定纵向纸张及公文字体；宽表通过分组、重复首列处理，输入预算为 32 列、1000 数据行，仍受全文 100000 字符预算约束。
