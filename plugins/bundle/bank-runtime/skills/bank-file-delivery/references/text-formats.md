# 文本与静态页面格式契约

只在当前目标确实要求对应文件时使用。生成和修订通过成果工具提交完整内容，文件内容不作为程序执行。字段与实际工具schema一致，不把格式能力当作新增目标。

## 文本格式

TXT使用artifact_type=txt及.txt扩展名，Markdown使用artifact_type=markdown及.md扩展名，content为完整字符串或仅含text的对象。代码围栏只在文件正文需要展示围栏时保留，不用围栏包装工具参数。

SQL使用artifact_type=sql、output_name以.sql结尾，content为完整SQL字符串或{"text":"SELECT 1;"}，原样保存为UTF-8文本，不连接数据库执行，不套Markdown代码围栏。格式支持仍须满足当前助手授权及Worker能力。

## 静态页面

HTML使用artifact_type=html、output_name以.html结尾，content为完整静态HTML字符串或仅含text的对象。支持页面外壳、正文、表格和受限静态样式；不支持脚本、事件属性、表单、交互控件、外链资源和CSS资源加载。需要交互而当前能力不支持时，说明限制，不默默改成交付静态版。

```json
{"artifact_type":"html","title":"内容示例","output_name":"内容示例.html","content":{"text":"<!doctype html><html><head><meta charset=\"utf-8\"><title>内容示例</title></head><body><h1>内容示例</h1><p>此处为结构示例。</p></body></html>"}}
```

校验失败按真实诊断修正对应内容后，在允许预算内使用同一成果工具重试。临时文件或脚本执行结果不能充当已发布成果，不绕过权限或活动内容限制。

## PDF

仅明确要求交付 PDF 时设置explicit_pdf_request=true；生成与对外交付转换均遵守此条件。内部读取转换使用purpose=read，无需虚构用户要求或explicit_pdf_request，也不主动发布为下载成果。

仅在用户已明确要求 PDF 时生成；普通 Word 请求不能因失败而改成 PDF。PDF 不携带 DOCX 专用的 delivery_plan。普通报告优先使用 sections/paragraphs，标题单独传 title，不在正文中重复顶层标题；复杂表格需使用当前已支持的排版能力，不假定旧版 PDF 渲染器会解析 Markdown。

```json
{"artifact_type":"pdf","title":"说明示例","output_name":"说明示例.pdf","content":{"sections":[{"heading":"测试目标","paragraphs":["检查文件内容是否完整，所有数据均为示例。"]},{"heading":"结论","paragraphs":["示例测试记录已汇总，待按实际结果核对。"]}]},"explicit_pdf_request":true}
```


直接PDF支持范围以当前工具契约为准；Office转PDF使用artifact_convert，保留已有文件版式，不把重写内容冒充转换。

## 已有文件转换

用户明确要求将已生成的 Word 转为 PDF。

```json
{"source_generated_file_id":"generated_example_only","target_format":"pdf","output_name":"转换示例.pdf","explicit_pdf_request":true}
```
