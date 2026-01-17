# 忘@了戳一下吧

**发消息忘了 @Bot？戳一戳就行。**

---

## 功能

在群聊中戳一戳 Bot，触发 LLM 响应。Bot 会根据最近的对话上下文，自动判断你的意图并回应。

### 适用场景

| 场景 | 效果 |
|------|------|
| 发消息忘了 @Bot | 戳一戳后 Bot 回应之前的内容 |
| 想和 Bot 聊天 | Bot 自然地开启对话 |
| 希望 Bot 参与当前话题 | Bot 根据上下文加入讨论 |
| 对 Bot 之前说的话有反应 | Bot 理解你在回应它 |
| 单纯戳着玩 | Bot 俏皮地回应 |

---

## 上下文机制

插件通过 AstrBot 标准 LLM 链路工作：

### 无 context_aware 插件时

使用**框架自带的对话历史**（`conversation.history`）：
- 包含 @Bot 的消息 + Bot 的回复
- 戳一戳触发的对话会自动记入历史
- 后续对话能看到这次交互

### 有 context_aware 插件时（推荐）

除框架对话历史外，还会：
- 正确识别戳一戳触发类型（`TRIGGER_POKE`）
- 生成正确的场景描述，显示「谁戳了你」而非群里最后一条消息
- 包含群里所有人的最近消息作为上下文
- Bot 能根据完整的群聊上下文判断用户意图

**无需额外配置**，插件会自动适配两种情况。

---

## 配置项

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `enable` | 启用插件 | `true` |
| `enable_in_groups` | 群聊中启用 | `true` |
| `enabled_groups` | 启用的群列表（留空=全部） | `[]` |
| `enable_in_private` | 私聊中启用 | `true` |
| `cooldown` | 冷却时间（秒） | `5.0` |
| `blacklisted_users` | 用户黑名单 | `[]` |
| `poke_prompt` | 提示词模板 | 见下方 |

### 默认提示词

```
{username}戳了戳你。

可能的情况：
- 刚才说话忘了@你，希望你回应之前的内容
- 想和你聊天
- 希望你参与当前话题或回答问题
- 对你之前说的话有反应
- 只是单纯戳你玩

请根据最近的对话上下文，判断用户意图并自然回应。如果上下文没有明确话题，可以俏皮地回应这个戳一戳。
```

`{username}` 会被替换为戳一戳用户的昵称。

---

## 技术细节

### 与 context_aware 插件协作

本插件会设置以下 extra 标记，供 context_aware 插件识别：

| 标记 | 说明 |
|------|------|
| `_poke_trigger` | 标记这是戳一戳触发 |
| `_poke_sender_id` | 戳一戳用户的 ID |
| `_poke_sender_name` | 戳一戳用户的昵称 |

context_aware 插件 v2.5.0+ 会识别这些标记，生成正确的场景描述。

### 对话记录流程

1. 戳一戳事件 → 插件获取当前 `conversation` 对象
2. 设置 `_poke_trigger` 等标记
3. 调用 `event.request_llm(prompt=..., conversation=conversation)`
4. context_aware 识别戳一戳触发，生成正确的场景描述
5. 框架从 `conversation.history` 加载对话历史
6. LLM 生成回复
7. 框架自动将新对话保存到 `conversation.history`

### 内存管理

- 冷却记录超过 10 分钟自动清理
- 每 50 次戳一戳触发一次清理
- 无内存泄漏风险

---

## 版本历史

### v2.1.0
- 新增 `_poke_trigger`、`_poke_sender_id`、`_poke_sender_name` 标记
- 与 context_aware v2.5.0 协作，正确显示戳一戳用户信息

### v2.0.0
- 初始版本
- 通过标准 LLM 链路触发回复
- 自动兼容框架对话历史和 context_aware 插件

---

## 注意事项

- 仅支持 aiocqhttp 平台（NapCat / go-cqhttp）
- 冷却时间防止同一用户频繁触发
- 对话会正常记入对话历史
- 推荐配合 context_aware 插件使用以获得最佳效果
- 如果同时安装了 `astrbot_plugin_llm_poke`，建议禁用其一，避免重复响应
