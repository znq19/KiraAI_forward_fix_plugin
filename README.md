# KiraAI Forward Fix（合并转发修复插件）

> 无感知修复 KiraAI 内置 `<forward>` 合并转发功能，让机器人能正常把多条消息合并转发到群聊 / 私聊。

## 这是什么？为什么要用它？

KiraAI 内置的合并转发功能存在一个缺陷：当机器人使用 `<forward merge="true">` 标签合并转发多条消息时，OneBot 会报错：

```
retcode 1400: message segment "node" is only valid inside a forward node list
```

**原因**：内置实现把合并转发的 node 消息段当作普通消息发送（`send_group_msg`），而 OneBot 规定 node 段只能出现在合并转发专用接口（`send_group_forward_msg` / `send_private_forward_msg`）中。

**本插件**在消息发送前拦截合并转发请求，改用 OneBot 专用接口发送，从而修复该问题。

> **💡 本插件最大的意义**：解决**非 NapCat**（如 **SnowLuma**、**LLOneBot**）配合 KiraAI 无法正常合并转发的问题。
> NapCat 走 NTQQ 客户端数据库，内置实现勉强可用；但 SnowLuma / LLOneBot 只在自己的消息存储中反查消息，内置实现（按 ID 引用节点）必然失败。本插件改用**内容节点**（自带发送者信息，零反查），跨实现可靠。

## 特性

- ✅ **对 LLM 完全透明**：不注册新工具、不修改提示词，机器人完全无感知
- ✅ **对官方逻辑零侵入**：只处理合并转发，其他消息原样放行
- ✅ **对其他插件无影响**：高优先级执行，只匹配 Forward 类型
- ✅ **完全静默**：默认成功/失败都不发提示文本（可配置）
- ✅ **群聊 / 私聊都支持**：自动根据会话类型选择对应接口
- ✅ **媒体真实转发**：所有普通消息（文本/图片/语音/视频/表情）走内容节点原样重发（含真实昵称，群名片优先），不依赖消息 ID 反查，兼容 NapCat / SnowLuma / LLOneBot
- ✅ **嵌套转发**：转发已有的聊天记录（多层转发）时，forward 段保留在内容节点内，QQ 客户端原生渲染嵌套卡片
- ✅ **引用真实显示**：reply 段先探测被引用消息是否可解析——可解析保留原生引用气泡（内容+时间正确），不可解析文本化 `[引用 msg_id:xxx]` 保底，显示永不出错
- ✅ **指哪打哪**：历史窗口外的消息 ID 逐个调 `get_msg` 精确解析，不再回退猜最近 N 条；仅当大部分 ID 无法解析时才回退（防 LLM 幻觉 ID）
- ✅ **零配置**：装上即用，自动识别 QQ 平台

## 安装

1. 下载最新版插件压缩包（`forward_fix_plugin_v1.5.0.zip`）
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

### v1.5.6（2026-09-03）

- **修复引用错位 + SnowLuma reply 报错（根因实锤）**：v1.5.2 起把 reply 段改写为 `{id: seq, seq: seq}`（seq=探测的 `message_seq`），但 **NapCat 的 `get_msg` 把 `message_seq` 覆盖成短哈希 message_id**（`action/msg/GetMsg.ts`：`retMsg.message_seq = retMsg.message_id`）——它不是 QQ 权威 seq！NapCat 发送侧 `seq` 分支用假 seq 查 `getMsgsBySeqAndCount` → **查到错误消息 → 引用错位**；SnowLuma 的 `resolveReplySequence(messageId)` 用 message_id 反查 store，传 seq 进去反查失败 → 旧版抛 `message segment "reply" is missing required or usable fields`
- **修复**：reply 段**保留原始 message_id，不再改写 seq**——NapCat 走 `id` 分支 `getMsgIdAndPeerByShortId` 反查真实消息、SnowLuma 走 `resolveReplySequence(id)` 反查 store、LLOneBot 标准兼容（这就是 LLOneBot 引用完全真实的原因）
- **不丢弃**：不可解析的 reply 段**剔除段、保留消息其余内容**（不再整条跳过）；探测闭环不变（get_msg 能查到 ⇔ reply 能渲染，同一消息存储）

### v1.5.5（2026-09-03）

- **策略反转：ID 节点优先 + 内容节点回退**（分流）——之前"内容节点优先"反而破坏了 NapCat 的原生能力（不装插件时 KiraAI 直接发 node 段，NapCat 从客户端数据库反查，图片/文件/嵌套全真实）。现在：**先发 ID 节点**（NapCat/LLOneBot 第一次就成功，与不装插件行为完全一致）；**失败自动回退内容节点**（SnowLuma 路径：file/music 段剔除、reply 改写权威 seq、嵌套 forward 展开）；回退仍失败再剔除 reply 节点重试
- **回退路径强制内容节点**：`_build_node` 加 `force_content` 参数——ID 节点发送已失败时，file/music 消息也重建为内容节点（段剔除、文本保留），不再放回 ID 节点（会因同样原因再挂）

### v1.5.4（2026-09-03）

- **修复**：NapCat 下转发整体失败（`retcode 1200: element not found`，栈 `handleOb11FileLikeMessage` → `at async file`）——内容节点里的 **file 段** NapCat 会尝试下载 `data.url`，历史消息的 url 通常已过期 → 下载失败 → **拖垮整个转发**。修复：内容节点构建时 **file/music 段一律剔除**（外层 file 消息走 ID 节点，NapCat 从客户端数据库反查真实文件，不依赖 url 下载）；image/record/video 有源保留（NapCat 实测正常）
- **规范**：manifest 补 `repo` 字段

### v1.5.3（2026-09-03）

- **修复**：嵌套转发"点进去是空的"——forward 段不再保留 `{id: message_id}`（会被实现当 res_id 用，指向不存在的资源 → 空卡片），改为调 `get_forward_msg` 展开成**嵌套内容节点**（纯 node 数组），SnowLuma 的 `uploadRecursive` / NapCat 的 `uploadForwardedNodesPacket` 原生渲染多层转发卡片
- **修复**：转发卡片时间显示 1970——节点 `time` 字段**总是提供**（消息时间或当前时间兜底），旧版 SnowLuma 省略 time 会渲染 1970
- **保持**：reply 段改写 `{id: seq, seq: seq}`（QQ 权威序列），SnowLuma 渲染真实引用跳转、NapCat 按 seq 查真实消息显示完整引用条

### v1.5.2（2026-09-03）

- **修复**：引用消息"成功且真实"——reply 段探测升级为**提取被引用消息的 QQ 权威序列（message_seq）**，并改写为 `{id: seq, seq: seq}`，三端全部真实渲染：
  - **SnowLuma**：reply codec 读 `data.id`，正数直接当 QQ 序列渲染真实引用气泡（旧版对负数 message_id 哈希会报 1400——这是之前转发失败的根因）
  - **NapCat**：reply 转换优先用 `data.seq` 按序列查真实消息
  - **LLOneBot**：标准 OneBot reply 段，id=seq 兼容
- **修复**：被引用消息无法解析（get_msg 失败或无 message_seq）时**整条跳过**——绝不文本化、绝不发送残缺 reply，转发的每条消息都是真实引用
- **保持**：发送失败时剔除含 reply 的节点重试一次（不文本化），保证其余消息真实转发成功

### v1.5.1（2026-09-03）

- **修复**：引用消息显示真实样子——reply 段先调 `get_msg` 探测被引用消息是否可解析
  - 可解析（NapCat 走客户端数据库几乎总是；SnowLuma 被引用消息在 store 内）→ **保留原生 reply 段**，QQ 渲染真实引用气泡（内容 + 时间正确）
  - 不可解析 → 文本化 `[引用 msg_id:xxx]` 保底，显示永不出错
- **原理**：get_msg 与 reply 反查走同一消息存储（SnowLuma 的 findMeta），get_msg 能查到 ⇔ reply 段能正常渲染

### v1.5.0（2026-09-02）

- **新增**：嵌套转发支持——转发已有的聊天记录（多层转发）时，forward 段保留在内容节点内，QQ 客户端原生渲染嵌套卡片（NapCat / SnowLuma 均支持）
- **修复**：引用消息的时间/引用显示——reply 段文本化为 `[引用 msg_id:xxx]`，不依赖实现的反查（SnowLuma 反查失败会显示错误）；`time` 为 0 时省略字段，避免显示 1970
- **优化**：README 说明本插件最大意义是解决非 NapCat（SnowLuma / LLOneBot）配合 KiraAI 无法正常合并转发的问题

### v1.4.3（2026-09-02）

- **修复**：回退转发时报 `message element "image" requires a file/url source`
  - 原因：SnowLuma 存储的历史消息中图片段可能缺 `url`（rkey 过期/未解析），直接进内容节点会整体 1400
  - 修复：内容节点构建时剔除无源的媒体段（image/record/video），全部剔除则该消息跳过
- **修复**：SnowLuma 历史消息 `time` 为 0 时排序失效导致回退取错消息——仅当时间戳有效时才重排，否则保持实现返回顺序

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
