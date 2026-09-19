"""Fixed public recovery guidance; never expose exception text or paths."""
_HINTS = {
    "DOCUMENT_ARGUMENT_INVALID": "检查 inventory 中的工作表和列名；columns 不可重复，行范围须为正整数且起点不大于终点，metrics 须指定有效列和统计函数。修正参数后重试。",
    "DOCUMENT_FORMULA_CACHE_MISSING": "工作簿含缺失或错误的公式缓存。请在 Excel 中重新计算并保存，再上传文件；当前结果不能用于完整统计。",
    "DOCUMENT_REF_EXPIRED": "文档结果已过期，请使用当前任务的 file_id 和 file_ref 重新解析，再使用新 document_ref 读取。",
    "DOCUMENT_RESULT_TOO_LARGE": "结果超过预算；读取时减少行列或分组。解析时拆分文件或清理任务派生结果后重试。",
    "FILE_REF_INVALID": "检查当前任务的文件引用；使用解析返回的 document_ref 和原样复制的 next_cursor，不要构造引用或路径。",
    "FILE_ACCESS_DENIED": "当前任务没有文件访问权限，请重新选择获授权的文件。",
    "FILE_TYPE_UNSUPPORTED": "请转换为支持的文件格式后重新上传。",
    "MINERU_SUBMIT_AMBIGUOUS": "解析提交状态不明，请先核查服务端状态，避免重复提交。",
}

def recovery_hint(code: str) -> str:
    return _HINTS.get(code, "读取未完成，请检查文件和解析服务状态后重试；不要依据不完整结果给出全量结论。")
