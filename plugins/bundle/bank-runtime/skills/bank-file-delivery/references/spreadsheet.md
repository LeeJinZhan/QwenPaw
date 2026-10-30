# 表格格式契约

适用于已确定的XLSX或CSV文件交付；不代替材料分析，也不增加统计目标。

- XLSX 使用 sheets 数组，每项 name 与 rows 二维数组；headers 可单列提供。所有集合直接传数组，无 item 包装；content 对象不再次序列化成字符串。
- 可修改公式写成 {"formula":"=SUM(B2:B5)"}，仅引用本工作簿已提供范围，支持算术比较和 SUM/AVERAGE/MIN/MAX/COUNT/ROUND/ABS/IF/IFERROR/SUMIF/COUNTIF，可跨表引用。普通等号开头字符串按文本处理；不提供缓存值、外链、宏或未登记函数。生成器重算后才发布，宽表分页不删内容，极长单元格仍受 Excel 容量约束。

生成 Excel：金额为数值，表头只出现一次，不把汇总示例当真实数据。

```json
{"artifact_type":"xlsx","title":"汇总结构示例","output_name":"汇总结构示例.xlsx","content":{"sheets":[{"name":"结构示例","headers":["网点","金额（万元）"],"rows":[["示例网点A",100],["示例网点B",200],["示例合计",300]]}]}}
```


## CSV

使用artifact_type=csv，content为columns字符串数组及rows二维数组，不传CSV源码或再次编码的JSON字符串。金额保留数值类型，表头只出现一次；不要把示例值当作用户数据。

```json
{"artifact_type":"csv","title":"示例数据","output_name":"示例数据.csv","content":{"columns":["项目","数量"],"rows":[["示例",1]]}}
```

## 已有文件转换

将上传的旧版 Excel 转为可读取的 XLSX。

```json
{"source_type":"session_file","source_id":"uploaded_xls_example_only","target_format":"xlsx","purpose":"read","output_name":"转换示例.xlsx"}
```
