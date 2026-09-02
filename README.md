# KiraAI Forward Fix（合并转发修复插件）

> 无感知修复 KiraAI 内置 `<forward>` 合并转发功能，让机器人能正常把多条消息合并转发到群聊 / 私聊。

## 这是什么？为什么要用它？

KiraAI 内置的合并转发功能存在一个缺陷：当机器人使用 `<forward merge="true">` 标签合并转发多条消息时，OneBot 会报错：

```
retcode 1400: message segment "node" is only valid inside a forward node list
```

**原因**：内置实现把合并转发的 node 消息段当作普通消息发送（`send_group_msg`），而 OneBot 规定 node 段只能出现在合并转发专用接口（`send_group_forward_msg` / `send_private_forward_msg`）中。

**本插件**在消息发送前拦截合并转发请求，改用 OneBot 专用接口发送，从而修复该问题。

## 特性

- ✅ **对 LLM 完全透明**：不注册新工具、不修改提示词，机器人完全无感知
- ✅ **对官方逻辑零侵入**：只处理合并转发，其他消息原样放行
- ✅ **对其他插件无影响**：高优先级执行，只匹配 Forward 类型
- ✅ **完全静默**：默认成功/失败都不发提示文本（可配置）
- ✅ **群聊 / 私聊都支持**：自动根据会话类型选择对应接口
- ✅ **媒体真实转发**：所有普通消息（文本/图片/语音/视频/表情/引用）走内容节点原样重发（含真实昵称，群名片优先），不依赖消息 ID 反查，兼容 NapCat / SnowLuma / LLOneBot；文件/嵌套转发/音乐卡片保留真实 ID 节点，结构完整
- ✅ **指哪打哪**：历史窗口外的消息 ID 逐个调 `get_msg` 精确解析，不再回退猜最近 N 条；仅当大部分 ID 无法解析时才回退（防 LLM 幻觉 ID）
- ✅ **零配置**：装上即用，自动识别 QQ 平台

## 安装

1. 下载最新版插件压缩包（`forward_fix_plugin_v1.1.0.zip`）
2. 解压到 KiraAI 的 `data/plugins/` 目录下，确保目录结构为：

```
data/plugins/
└── forward_fix/
    ├── main.py
    ├── manifest.json
    ├── schema.json
    └── icon.png
```

3. 在 KiraAI 的插件管理页面（或 `data/config/plugins.json`）中启用 `forward_fix` 插件
4. 重启 KiraAI（或触发插件热重载）

## 使用

**无需任何配置**。安装并启用后，让机器人合并转发消息即可：

> 用户：把群里最近 10 条消息合并转发到这个群

机器人会正常输出 `<forward merge="true">消息ID列表</forward>`，插件自动拦截并改用 OneBot 专用接口发送。

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `silent_fail` | 开关 | `true` | 发送失败时是否静默。开启：只记日志，不打扰用户；关闭：失败时向会话发送一条提示文本 |

## 工作原理

```
LLM 输出 <forward merge="true">id1,id2</forward>
        ↓
内置 ForwardTag 解析为 Forward 元素（MessageChain 内）
        ↓
本插件在 after_xml_parse 阶段拦截：
  1. 从消息链中移除 Forward 元素
  2. 自动拉取真实历史（get_group_msg_history / get_friend_msg_history）
  3. 匹配消息 ID，构造内容节点：
     - 普通消息（含引用回复）→ 内容节点（user_id + nickname + time + content）原样重发
     - 文件/嵌套转发/音乐卡片 → 真实 ID 节点保留结构
     - 历史窗口外的 ID → 逐个调 get_msg 精确解析，查不到才跳过
  4. 直接调用 OneBot 专用接口：
     - 群聊 → send_group_forward_msg
     - 私聊 → send_private_forward_msg
        ↓
OneBot 正常发送合并转发 ✅
```

> **为什么不用纯 ID 节点？** 按 ID 引用消息时，OneBot 实现需要反查发送者：NapCat 走 NTQQ 客户端数据库（能查到），但 SnowLuma / LLOneBot 只在自己的消息存储（仅含启动后收到的消息）中反查，历史接口返回的 ID 不在其中就会报 `has no valid sender user_id`。内容节点自带发送者信息，不依赖反查，跨实现可靠。

## 常见问题

**Q：安装后没有效果？**
A：请检查：① 插件是否已启用；② 日志中是否出现 `[forward_fix]` 相关记录；③ 你的 OneBot 实现（如 NapCat）是否支持 `send_group_forward_msg` 接口。

**Q：会和其他插件冲突吗？**
A：不会。插件只处理 `Forward` 类型的消息元素，其他消息原样放行。

**Q：失败时会怎样？**
A：默认静默失败（只记日志）。如需用户可见的失败提示，可在插件配置中关闭 `silent_fail`。

## 开源协议

本项目基于 [GNU Affero General Public License v3.0](LICENSE) 开源。

<details>
<summary><b>更新日志</b></summary>

### v1.4.2（2026-09-02）

- **修复**：SnowLuma 下转发仍报 `forward node message_id xxx has no valid sender user_id`
  - 原因：纯引用消息（只有 reply 段）在 SnowLuma 中 reply 段转换失败被跳过 → `message` 数组为空 → 内容节点构建失败 → v1.4.1 回退 ID 节点 → 反查失败 → 整体 1400
  - 修复：内容节点构建失败时**跳过该消息**（不再回退 ID 节点），其余消息正常转发；发送失败且含 ID 节点时自动剔除 ID 节点重试一次，保证能转的消息一定转出去
- **修复**：LLM 轻微幻觉 ID（如把 `1159489171` 写成 `1159489101`）时，该条被跳过并记日志，不再影响其他消息

### v1.4.1（2026-09-02）

- **修复**：SnowLuma 下转发最近 10 条全部失败（`no nodes to send`）
  - 原因：部分实现（如 SnowLuma）历史消息把发送者放在 `sender.user_id` 嵌套结构，顶层 `user_id` 缺失 → 内容节点构建失败 → 节点列表为空
  - 修复：`user_id` 兼容 `sender.user_id` 嵌套结构；内容节点构建失败时回退为 ID 节点（实现能反查时仍可转发），不再静默丢弃

### v1.4.0（2026-09-02）

- **修复**：合并转发中部分用户不显示 QQ 昵称（显示为 QQ 号）
  - 原因：内容节点只传了 `user_id`，QQ 客户端无昵称时回退显示 QQ 号
  - 修复：内容节点补充 `nickname` 字段（群名片优先，其次昵称）
- **修复**：转发"指哪打哪"——LLM 引用历史窗口外的消息（如回复目标）时不再回退猜最近 N 条
  - 新增：历史中未匹配的 ID 逐个调 `get_msg(id)` 精确解析（NapCat 走客户端数据库、SnowLuma 走消息存储），查得到就精确转发，查不到才跳过
  - 仅当大部分 ID 都无法解析时才回退最近 N 条（防 LLM 幻觉 ID）

### v1.3.0（2026-09-02）

- **修复**：SnowLuma / LLOneBot 下合并转发仍报 `has no valid sender user_id`
- **根因**：按消息 ID 引用节点时，SnowLuma / LLOneBot 在自己的消息存储（仅含启动后收到的消息）中反查发送者，历史接口返回的 ID 不在其中即失败；NapCat 走 NTQQ 客户端数据库所以正常
- **方案**：内容节点优先——所有普通消息（含引用回复）一律构造完整内容节点（`user_id` + `time` + `content`）原样重发，完全不需要 ID 反查；仅文件/嵌套转发/音乐卡片保留 ID 节点
- **兼容性**：内容节点是 OneBot v11 标准格式，NapCat / SnowLuma / LLOneBot 均支持

### v1.2.0（2026-09-02）

- **修复**：合并转发报 `forward node message_id xxx has no valid sender user_id`（retcode 1400）
- **根因**：按消息 ID 引用节点时，NapCat 需反查每条消息的发送者，ID 不在缓存（消息较旧/跨会话/LLM 幻觉 ID）即失败
- **方案**：改为混合节点——发送前自动拉取真实历史（`get_group_msg_history` / `get_friend_msg_history`），普通消息（文本/图片/语音/视频/表情）构造完整内容节点（`user_id` + `time` + `content`）原样重发，不依赖 ID 反查；文件/嵌套转发/引用/卡片/音乐等结构敏感消息保留真实 ID 节点
- **新增**：ID 匹配率低于 50% 时自动判定 LLM 幻觉 ID，回退为转发最近 N 条真实历史消息
- **新增**：节点 `user_id` 强制字符串类型（NapCat 要求 string，传数字会 1400）

### v1.1.0（2026-09-02）

- **修复**：平台判断改为大小写不敏感（兼容 `QQ` / `qq` 配置）
- **修复**：会话类型精确匹配 `gm`（群聊）/ `dm`（私聊），不再猜测未知类型
- **修复**：`send_action` 超时提升到 30 秒，兼容大消息合并转发
- **修复**：消息 ID 解析失败时保留原消息链交给内置发送器，避免静默吞消息
- **新增**：`silent_fail` 配置开关（默认开启，失败静默；关闭则发送提示文本）
- **新增**：适配器 / 客户端判空保护，失败时输出明确错误日志
- **优化**：注释全部改为英文，符合 KiraAI 插件开发规范

### v1.0.0（2026-08-26）

- 初始版本：拦截 `after_xml_parse` 阶段的 Forward 元素，直接调用 OneBot 合并转发专用接口
- 支持群聊 / 私聊合并转发

</details>
