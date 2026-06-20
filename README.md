# astrbot_plugin_heartflow_context

心流主动发言插件（Context Plus 依赖版）—— 利用 Prefix Caching 实现极低成本的智能群聊主动回复

> **必须与 `astrbot_plugin_context_plus` 捆绑使用，不可单独运行。**

## 核心特性

### 基于完整上下文的精准判断

复用 `context_plus` 的聊天日志系统（历史摘要、群成员画像、逐日聊天记录），让 LLM 在判断是否主动回复时拥有完整上下文，判断准确性大幅提升。

### 极致 Token 优化

每次判断只需约 **50 tokens** 的变化量（仅当前消息内容），其余 prompt 全部命中 Prefix Caching 缓存：

| 指标 | 数值 |
|------|------|
| 每次判断新增 tokens | ~50 |
| 缓存命中率 | **95%-97%** |

### 共享人设与角色

自动读取 `context_plus` 保存的群聊人设文件，确保主动发言与被动回复的角色一致。

## 工作原理

```
群聊消息
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  心流判断器（复用主 LLM 的缓存）                      │
│                                                     │
│  system_prompt（已缓存）：                            │
│  ├── 人设（从 context_plus 读取）                     │
│  ├── 历史摘要（天级变化，缓存友好）                    │
│  ├── 群成员画像（天级变化，缓存友好）                  │
│  ├── 历史聊天日志（天级变化，缓存友好）                │
│  ├── 当天日志（末尾追加，前缀缓存友好）                │
│  └── 固定评分指令（完全不变，始终缓存）                │
│                                                     │
│  user_content（本次新增 ~50 tokens）：                │
│  └── 活跃度 + 发送者 + 消息内容                      │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
               评分 ≥ 阈值？──否──→ 跳过
                       │
                       是
                       ▼
               注入主动发言提示
                       │
                       ▼
               主 LLM 生成回复
                       │
                       ▼
               更新群聊状态（活跃度等）
```

## 安装要求

### 必要条件

1. **必须安装并启用** `astrbot_plugin_context_plus`
2. **必须使用支持 Prefix Caching 的模型**（如 DeepSeek）
3. 判断模型提供商必须和主模型**相同**（共享缓存）

### 安装步骤

1. 将本插件放置在 AstrBot 的 `data/plugins/` 目录
2. 确保 `astrbot_plugin_context_plus` 已安装并正常运行
3. 重启 AstrBot 加载插件
4. 在 WebUI 中配置插件参数

## 配置说明

### 基本配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_heartflow` | bool | false | 启用心流主动发言功能 |
| `judge_provider_name` | string | "" | 判断模型提供商 ID（必须和主模型相同） |

### 心流参数

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `reply_threshold` | float | 0.6 | 回复阈值（0-1，越高越严格） |
| `activity_target_messages` | int | 10 | 达到满活跃度需要的消息数量 |
| `activity_decay_rate` | float | 0.1 | 每次回复后活跃度下降速率 |
| `activity_recovery_rate` | float | 0.02 | 每分钟活跃度恢复速率 |
| `activity_min_threshold` | float | 0.3 | 活跃度最低阈值，低于此值回复意愿显著降低 |
| `min_reply_interval_seconds` | int | 20 | 两次主动回复的最小间隔（秒） |

### 白名单配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `whitelist_enabled` | bool | false | 是否启用白名单 |
| `chat_whitelist` | list | [] | 群组白名单列表 |

### 判断权重

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `judge_relevance` | float | 0.25 | 消息相关性（是否有趣、有回复价值） |
| `judge_willingness` | float | 0.20 | 回复意愿（基于当前活跃度） |
| `judge_social` | float | 0.20 | 社交氛围（回复是否合时宜） |
| `judge_timing` | float | 0.15 | 回复时机（是否恰当） |
| `judge_continuity` | float | 0.20 | 话题连贯性（与当前话题相关度） |

权重会自动归一化，确保总和为 1。

### 调试配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `judge_include_reasoning` | bool | false | 日志中显示判断理由 |
| `debug_thinking_mode` | bool | false | 检查思考模式是否关闭 |
| `log_full_request` | bool | false | 记录完整 system_prompt 用于调试 |

### 配置说明

> **聊天日志相关配置（`chat_log_max_chars`、`chat_log_days`）无需手动设置。**
> 本插件会自动读取 `context_plus` 的配置，确保两者保持一致，最大化缓存命中率。

## 活跃度系统

活跃度控制机器人的"社交意愿"，是本插件的核心状态机制：

| 机制 | 触发条件 | 效果 |
|------|----------|------|
| **活跃度上升** | 群聊消息增多 | 机器人更倾向于参与话题 |
| **活跃度消耗** | 机器人主动回复后 | 避免连续刷屏 |
| **活跃度恢复** | 时间流逝 | 冷场群也能缓慢恢复，避免永久沉默 |
| **每日保底** | 每天 00:00 | 重置活跃度不低于 0.5 |

## 管理命令

- `/heartflow` — 查看当前群聊的心流状态（活跃度、统计信息等）

## 常见问题

### 插件不回复任何消息

1. 确认 `enable_heartflow` 设为 `true`
2. 确认 `judge_provider_name` 配置正确（与主模型相同的提供商 ID）
3. 确认已安装并启用 `astrbot_plugin_context_plus`
4. 检查白名单配置
5. 查看 AstrBot 日志中的错误信息

### 如何查看可用提供商 ID？

启动 AstrBot 后，插件会在日志中输出所有可用的提供商 ID：

```
[HeartflowContext] 可用的提供商 ID: ['deepseek-chat', 'openai-gpt4']
```

### 缓存命中率低？

- 确认 `judge_provider_name` 与主模型使用**同一提供商**
- 确认模型支持 Prefix Caching（如 DeepSeek）
- 检查 `context_plus` 的聊天日志配置是否稳定

### 回复过于频繁或太少

- 调高 `reply_threshold` 减少回复，调低增加回复
- 调整 `activity_decay_rate` 和 `activity_recovery_rate` 控制活跃度变化速度
- 调高 `min_reply_interval_seconds` 增加回复间隔

## 日志说明

| 日志前缀 | 说明 |
|----------|------|
| `心流触发回复` | 成功触发主动回复 |
| `心流评分` | 每次判断的详细评分 |
| `缓存命中率` | 每次 LLM 调用的缓存命中率 |
| `已读取 context_plus 配置` | 成功读取依赖配置 |

## 许可证

本插件遵循 AstrBot 的开源许可证。
