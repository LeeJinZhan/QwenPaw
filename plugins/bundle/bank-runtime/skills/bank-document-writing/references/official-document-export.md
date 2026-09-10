# 公文交付契约维护参考

与 SKILL.md 内交付规则同步；运行时不要求读取此参考文件。

## 公文 DOCX 交付

普通文字与公文写作由当前助手直接完成，不因为文种委派专家。用户只要正文时直接交付正文。要求公文 DOCX 时调用当前获授权的 `artifact_generate`；以下是完整参数，content 直接提交对象，kind 和 layout_version 位于 content 内、document 外：

```json
{"artifact_type":"docx","title":"工作安排通知初稿示例","output_name":"工作安排通知初稿示例.docx","delivery_plan":{"document_type":"notice","target_format":"docx","layout_kind":"official_document"},"content":{"kind":"official_document","layout_version":"bank-official-docx-v1","document":{"title":"关于工作安排的通知","recipients":["各测试小组"],"blocks":[{"type":"heading","level":1,"text":"一、工作安排"},{"type":"paragraph","text":"测试完成后，各组汇总结果并提交测试记录。"}]}}}
```

此为字段示例，不是用户事实。document 可选字段：signatory、date、classification、urgency 为字符串，attachments、cc 为字符串数组。blocks 支持 paragraph（text）、heading（level 为 1–4 的整数、text）与 table（headers 为非空字符串数组、rows 为等宽字符串二维数组）。正文每个语义段落一个块，不合并丢段；数组直接传数组，不加 item、不转成 JSON 字符串或代码块。所有主送和抄送完整保留；recipients 每项填写一个单位名称，不自行添加冒号、换行或放进正文 blocks，主送段落由渲染器排版。布局由服务端固定，不传字体、路径、脚本、外链、命令或身份。

无明确事实不补日期、密级、文号、署名、附件或抄送。可以交付初稿时说明缺项，关键缺项影响实质内容时集中澄清。以“公文初稿”命名交付，不假冒签发、审批或正式定密。固定版式不是已发布机构模板。

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
