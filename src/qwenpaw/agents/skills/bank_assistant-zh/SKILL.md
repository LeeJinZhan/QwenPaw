---
name: bank_assistant
description: "当用户提出银行内部业务问题、客户查询、授信查询、风险预警、贷后跟进或报告草稿请求时使用本 skill，并通过 bank_assistant 工具执行受控银行能力。"
metadata:
  builtin_skill_version: "1.0"
  qwenpaw:
    requires: {}
---

# 银行助手

当用户以银行内部员工身份请求客户画像、授信摘要、风险预警、贷后跟进建议或受控报告草稿时，使用 `bank_assistant` 工具。

## 使用要求

- 只处理银行内部业务查询，不用于开放互联网检索、个人闲聊或通用文件处理。
- 调用前必须传入可信员工身份 `identity_json`，其中包含 `user_id`、`display_name`、`roles`、`org_id` 和 `allowed_customer_ids`。
- 能识别客户号时传入 `customer_id`；客户号缺失时仍可调用工具，由受控服务返回补充客户号提示。
- 不要绕过工具直接编造客户信息、授信数据、风险预警、审计结果或产物引用。
- 不要调用 shell、文件、浏览器、MCP、插件或任意外部工具访问银行客户数据。
- 工具返回 `allowed=false`、`PARTIAL_DENIED` 或拒绝 reason code 时，向用户说明未授权或部分未授权，不要重试规避权限边界。

## 示例

用户：查询 cust-001 的风险预警和授信情况。

调用：

```json
{
  "message": "查询 cust-001 的风险预警和授信情况",
  "customer_id": "cust-001",
  "session_id": "<当前会话ID>",
  "identity_json": "<可信员工身份JSON>"
}
```
