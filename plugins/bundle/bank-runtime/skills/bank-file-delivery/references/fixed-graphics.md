# 固定图形格式契约

适用于已要求的静态图形文件。可编辑图表制作及导出现有图表使用bank-chart，不重新生成来冒充原图导出。

- PNG/JPEG/WEBP/SVG 仅支持确定性的 chart、table、flowchart、cover。chart 字段为 kind、chart_type、title、categories、series 和可选 style_profile；商务风可用 executive。示例结构：`{"kind":"chart","chart_type":"bar","title":"示例","categories":["一月"],"series":[{"name":"数量","values":[1]}]}`。不猜坐标或样式字段，不传自然语言图片提示词、SVG 源码、URL 或路径；cover 仅用于目标需要的标题图。

内容结构必须遵循当前工具schema。只在用户目标符合支持的图形种类时使用，不以封面代替任意图片生成要求；工具没有所需能力时说明限制。
