# 公文交付契约维护参考

与 SKILL.md 内交付规则同步；运行时不要求读取此参考文件。

## 公文 DOCX 交付

普通文字与公文写作由当前助手直接完成，不因为文种委派专家。用户只要正文时直接交付正文。要求公文 DOCX 时调用当前获授权的 `artifact_generate`；以下是完整参数，content 直接提交对象，kind 和 layout_version 位于 content 内、document 外：

```json
{"artifact_type":"docx","title":"工作安排通知初稿示例","output_name":"工作安排通知初稿示例.docx","delivery_plan":{"document_type":"notice","target_format":"docx","layout_kind":"official_document"},"content":{"kind":"official_document","layout_version":"bank-official-docx-v1","document":{"title":"关于工作安排的通知","recipients":[],"blocks":[{"type":"heading","level":1,"text":"一、工作安排"},{"type":"paragraph","text":"本段仅为结构示例，实际使用用户确认的安排，不补造执行时间或责任人。"}]}}}
```

此为字段示例，不是用户事实。document 可选字段：signatory、date、classification、urgency 为字符串，attachments、cc 为字符串数组。blocks 支持 paragraph（text）、heading（level 为 1–4 的整数、text）与 table（headers 为非空字符串数组、rows 为等宽字符串二维数组）。正文每个语义段落一个块，不合并丢段；数组直接传数组，不加 item、不转成 JSON 字符串或代码块。所有主送和抄送完整保留。布局由服务端固定，不传字体、路径、脚本、外链、命令或身份。

无明确事实不补日期、密级、文号、署名、附件或抄送。可以交付初稿时说明缺项，关键缺项影响实质内容时集中澄清。以“公文初稿”命名交付，不假冒签发、审批或正式定密。固定版式不是已发布机构模板。

用户明确指定机构模板时，仅用已获授权的真实已发布版本调用 `template_fill_docx`，使用实际字段 schema。模板未发布、停用或无权时说明限制，不以固定版式规避；用户未指定机构模板时才使用上述固定版式。

后续修改使用 `artifact_revise`，携带已有受控文件引用及完整修订结构。外层文件名可变化，`document.title` 保持用户确认的正文标题；不得覆盖旧文件。要求普通 Word 而非公文时使用完整正文字符串或普通 DOCX sections/paragraphs 对象。

生成成功且实际返回当前文件引用后，才说明文件已生成，由原文件卡片和工作区交付。缺字体、未知版式或校验失败时保留可用正文，准确说明尚未交付文件，不静默降级或改用 shell、write_file 等绕过。结构输入错误可据工具给出的安全提示修正后通过同一工具重试；权限拒绝不得规避。已发布文件和后续回答失败分别说明。


修订完整参数示例：`generated_example_only` 仅为结构占位，调用前必须替换为本轮可用且已授权的真实成果引用；没有该引用时不试填编号。content 包含未修改的全部段落；不添加 artifact_type、title 或 source_refs 等修订工具不接受的外层字段。

```json
{"source_generated_file_id":"generated_example_only","instructions":"按用户要求更新安排，保留其余完整正文","output_name":"工作安排通知修订初稿示例.docx","delivery_plan":{"document_type":"notice","target_format":"docx","layout_kind":"official_document"},"content":{"kind":"official_document","layout_version":"bank-official-docx-v1","document":{"title":"关于工作安排的通知","recipients":[],"blocks":[{"type":"heading","level":1,"text":"一、工作安排"},{"type":"paragraph","text":"修订示例：按用户已确认的安排更新本段，其余标题和完整正文保持原有要求。"}]}}}
```
