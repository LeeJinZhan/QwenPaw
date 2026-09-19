# QwenPaw Console 本地集成维护

> 文档版本：v1.0
> 日期：2026-09-08
> 适用范围：银行集成分支的 Skill 与 MCP 频道选择器；不改变后端权限契约。

## 修订记录

| 版本 | 日期 | 修订人 | 变更类型 | 变更摘要 | 影响范围 |
| --- | --- | --- | --- | --- | --- |
| v1.0 | 2026-09-08 | LeeJinZhan | 新建 | 记录银行频道与自定义频道选项修复、清空语义和验证入口。 | Console Skill、MCP |

## 频道配置

- 平台请求使用 `bank-runtime`，QwenPaw 控制台使用 `console`。Skill 的适用频道与 MCP 权限规则均提供 `bank-runtime` 候选项。
- 编辑期间保留已载入的自定义频道候选；删除选中值或规则后仍可重新选择。候选项保留不自动创建权限规则，也不代表该频道已安装或获授权。
- Skill 清空频道后保存，后端按 `all` 处理；页面明确提示适用于所有频道。若仅供平台调用，应选择 `bank-runtime`。
- MCP 的“所有频道”为 `*`；删除规则后的行为取决于剩余规则和默认策略，本次不修改该语义。不要用扩大适用范围代替修复频道选择。
- 此修复不会自动恢复先前已保存删除的配置。更新 Console 后按原意重新选择并保存；已保存的自定义频道全部删除并重载页面后，不承诺保留历史候选。

## 验证

在本目录执行：

```bash
npm run test:run -- src/hooks/useRetainedChannelValues.test.ts src/pages/Agent/MCP/accessPolicy.test.ts src/pages/Agent/Skills
npm run build
```

内网需交付更新后的 QwenPaw Console 静态资源；仅修改 Skill 正文不会更新频道选择器。现场验证删除、重选、保存并重新打开 `bank-runtime`，并确认限制为该频道的技能和 MCP 规则未变为全频道。
