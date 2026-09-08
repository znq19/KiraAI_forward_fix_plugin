# KiraAI Forward Fix v2.0.0（合并转发修复）

> 让 KiraAI 的 `<forward>` 合并转发在 **NapCat / LLOneBot / SnowLuma** 三家 OneBot 实现上**全部真实生效**：
> 嵌套转发、引用气泡（内容/时间/昵称）、头像与昵称、图片/语音/视频/表情/名片/卡片/文件。

## 为什么需要它

KiraAI 内置的 `<forward>` 标签会把 `{"type":"node"}` 段塞进 `send_group_msg`，OneBot 规定 node 段只能出现在合并转发专用接口里 → `retcode 1400`。

本插件在 `after_xml_parse` 阶段拦截，改用 `send_group_forward_msg` / `send_private_forward_msg` 发送，并针对三家实现各自的节点解析规则选择最优节点形态。

## 与旧版（v1.5.x）的区别

旧版策略是"**先发 ID 节点，失败再整体回退内容节点**"。这在 SnowLuma 上是错的：SnowLuma 遇到**无法解析的 id 节点会直接让整条转发失败**（`INVALID_FIELD: forward node message_id not found`），于是每次转发都要先失败一次再重试，而且回退后的内容节点会丢失文件、嵌套卡片和原生引用。

v2 改为：

1. **先探测实现**（`get_version_info` → `NapCat.Onebot` / `LLOneBot` / `SnowLuma`）；
2. **逐条消息选择节点形态**，绝不把 SnowLuma 解析不了的 id 交给它；
3. 发送失败时按**分级回退**（全内容节点 → 去掉引用 → 去掉嵌套），而不是整体重来。

## 三家实现的行为（源码实锤）

| | id 节点未命中 | 内容节点发送者 | 嵌套转发 | 内容节点文件 | 引用段 |
|---|---|---|---|---|---|
| **NapCat** | 静默跳过该节点 | packet 模式保留 `user_id/nickname/time`；非 packet 模式**变成机器人身份** | 原生递归（≤3 层） | 会下载 `data.url`，历史 URL 过期会拖垮整条转发 → 内容节点一律剔除 | 只认 `id`（全局唯一表），传 `seq` 会按**目标会话**查序列 |
| **LLOneBot** | 静默跳过该节点 | 保留 `uin/name/time` | 原生递归 | 需要 `url`/本地路径 | 只认 `id`（自己的 shortId），查不到只跳过该段 |
| **SnowLuma** | **整条转发硬失败** | 保留 `user_id/nickname/time` | 原生递归（≤3 层） | `file_id` 或可加载 `url` 均可（会用 url 重新上传到目标会话） | `id` 经 store 解析，解析不到则该段跳过 |

因此 v2 的选择是：

- **NapCat / LLOneBot**：优先 **id 节点**（原生复用原消息：文件、嵌套卡片、引用气泡全部保真），并把 `user_id/nickname/time` 一并带上做兜底；失败后回退内容节点。
- **SnowLuma**：**内容节点优先**。原因是它的 id 节点路径同样要把每个元素重新打包上传，但用的是 **store 里的旧副本**（图片 URL 不会刷新）、引用段没有 resolver（正数 id 被当序列）、嵌套卡片没有 piggyback uuid（收方可能打不开）。我们自己重建内容节点可以：拿到 `get_msg` 刷新过的图片 URL、注入被引用消息的真实 `message_seq`、把嵌套转发展开成带 piggyback 的 innerForward 链。只有在内容节点完全无法构造时，才回退到 id 节点，且必须先过它的 forward 场景校验（`user_id>0`、video 不能有兄弟段、不能有 poke、c2c 每节点最多 1 个文件、无未知段型）。
- 未知实现：id 优先 + 内容回退（旧行为）。

## 功能清单

- ✅ 群聊 / 私聊（`send_group_forward_msg` / `send_private_forward_msg`）
- ✅ **嵌套转发**：id 节点走原生嵌套卡片；内容回退路径用 `get_forward_msg` 展开成纯 node 数组，深度按三家上限（3 层）截断，超深自动用 `[聊天记录]` 占位而不是失败
- ✅ **引用气泡真实**（按实现分流，这是 v2 的关键修正）：
  - **NapCat / LLOneBot**：引用段保留**原始 message_id**（NapCat 走全局唯一表、LLOneBot 走自己的 shortId store），**绝不写 `seq`**（NapCat 的 `get_msg` 会把 `message_seq` 覆盖成短 id，写 seq 会引用错位）；
  - **SnowLuma**：它的转发节点解析 `parseForwardNodes` 调 `parseMessage(content, false)` **不传 `resolveReplySequence`**，因此正数 reply id 会被当作 **QQ 序列号**；插件因此在 SnowLuma 路径下改为注入被引用消息的**真实 `message_seq`**，并把含引用的消息改走内容节点（id 节点里缓存的引用 id 是哈希，会被误读成序列）；
  - 所有实现都先 `get_msg` 探测引用目标，不可解析则丢弃（可选文本化），**绝不发送坏引用**；
  - SnowLuma 的文件段**只发 `url` 或只发 `file_id`，绝不同时发**（它的 `prepareForwardFileElement` 见到 file_id 就跳过 url 重传分支，文件不在目标作用域缓存时会直接抛错）。
- ✅ **头像与昵称**：节点携带真实 `user_id` + `nickname`（群名片优先）；id 节点由实现原生解析
- ✅ **每个节点独立时间**：`time` 为 unix 秒，总是提供（缺失用当前时间），不会渲染成 1970
- ✅ **全媒体**：图片 / 语音 / 视频 / 表情 / 商城表情 / 名片 / JSON 卡片 / markdown；无可用源的媒体段会被丢弃而不是让整条转发失败
- ✅ **文件**：NapCat/LLOneBot 走 id 节点原生转发；SnowLuma 内容节点用 `url`（重新上传）或 `file_id`
- ✅ **卡片外显**：`source / summary / prompt / news`（前 4 行"昵称: 预览"）
- ✅ **保序 + 去重**：严格按 LLM 给出的 id 顺序（旧版会把 `get_msg` 补的放最后导致乱序）
- ✅ **幻觉防护**：解析率 < 50% 时回退到最近 N 条真实历史（按时间升序）
- ✅ `merge="false"` 单条转发走 `forward_group_single_msg` / `forward_friend_single_msg`，不支持时自动降级为节点转发
- ✅ 完全静默（可配置失败提示）；对 LLM / 官方逻辑 / 其他插件零侵入

## 安装

1. 把本仓库内容（至少 `main.py` / `manifest.json` / `schema.json` / `icon.png`）放到 `data/plugins/forward_fix/`；
   `tests/`、`PLAN.md`、`README.md` 仅用于验证与说明，运行时不需要：

```
data/plugins/forward_fix/
├── main.py
├── manifest.json
├── schema.json
└── icon.png
```

2. 在插件管理页启用 `forward_fix`（或修改 `data/config/plugins.json`），重启或热重载。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
|--------|------|------|------|
| `silent_fail` | 开关 | `true` | 失败时只记日志；关闭后失败会向会话发一条提示 |
| `reply_mode` | 枚举 | `native` | `native`：引用可解析则保留原生气泡，不可解析丢弃；`drop`：一律丢弃引用段；`textify`：可解析时渲染为 `[引用 昵称: 内容]` 文本 |
| `max_depth` | 整数 | `3` | 嵌套转发展开层数上限（三家客户端都最多 3 层） |
| `prefer_content_nodes` | 开关 | `false` | 强制内容节点（排查问题用） |
| `debug` | 开关 | `false` | 记录每个被丢弃消息段的类型与异常堆栈 |
| `loss_report` | 开关 | `false` | 有内容无法转发时，转发成功后向会话发一条丢失清单（日志始终有 WARNING） |
| `debug_dump` | 开关 | `false` | 把实际发送的节点 JSON 写入日志，便于实机核对 |

**无需配置即可使用。** 日志中会打印 `[forward_fix] target=napcat ids=[...] merge=True` 以及节点形态与回退级别。

## 严格校验兜底（避免"一个段毁掉整条转发"）

SnowLuma 的校验是"要么整条成功、要么整条失败"，因此内容节点在构造时就会剔除会让它硬失败的段：
- `json` 的 `data` 为空/缺失（它的 codec 会抛 `INVALID_FIELD`）
- `mface` 缺 `emoji_id`
- `video` 带兄弟段（只保留 video）
- `poke` / `shake`（forward 场景禁止）
- 仅接收型段（`flashtransfer` / `onlinefile` / `flash_file`）
- 未知段型（它的 `parseMessage` 会抛 `UNKNOWN_TYPE`）
- c2c 节点内第二个及以后的 `file`
- 无可用源的 image / record / video / file

每个被剔除的段都会记日志（`debug` 开启后更详细），不会静默。

## 已知限制

1. **跨会话引用气泡**：QQ 的转发卡片里，引用段记录的是原消息在其**原会话**中的序列号。若把 A 群的消息转发到 B 群，QQ 客户端可能无法展开引用内容（显示为空/不可见）。这是 QQ 客户端行为，三家协议端都无法绕过；插件已做到"能解析就发原生引用、解析不了就丢弃"，不会出现错位引用。
2. **NapCat 非 packet 模式**（`packetBackend: "disable"`）下内容节点会丢失原发送者身份。插件默认优先 id 节点，因此只要 id 可解析就不受影响；id 全失败时才会落到内容节点。建议保持 NapCat 默认的 `packetBackend: "auto"`。
3. **嵌套深度上限 3 层**：三家协议端与 QQ 客户端一致，更深的层级会被截断（插件用 `[聊天记录]` 占位，不会报错）。

## 工作原理

```
LLM 输出 <forward merge="true">id1,id2</forward>
        ↓ 内置 ForwardTag → Forward 元素
插件在 after_xml_parse 拦截：
  1. 探测实现（get_version_info）
  2. 拉历史 + 逐条 get_msg 解析 id（保序、去重、幻觉防护）
  3. 逐条选节点：
       NapCat/LLOneBot → id 节点（带 user_id/nickname/time 兜底）
       SnowLuma        → 先验证 id 是否可解析且通过 forward 校验，否则内容节点
  4. 调 send_group_forward_msg / send_private_forward_msg（含卡片外显）
  5. 失败分级回退：全内容节点 → 去引用 → 去嵌套
```

## 验证

**① 规则矩阵测试**：`tests/` 内含 KiraAI 框架 mock + 按三家源码规则编写的 OneBot 模拟器（id 命中/未命中、硬失败、嵌套深度、video 策略、poke 策略、文件源要求、引用 id/seq、json/mface 必填、丢失清单等），共 **53 项断言全部通过**：

```bash
python3 tests/test_matrix.py    # 53 项规则矩阵
```

覆盖：三家 × {文本、图片、语音、视频、文件（url/file_id/无源）、引用（可解析/不可解析/textify/drop）、嵌套（1 层/4 层/展开失败）、poke、视频+兄弟段、ghost id、幻觉 id、merge=false、私聊无历史接口、未知实现、非 QQ 平台}。

**② 超集回归测试（对真实 v1.5.6 代码）**：`tests/test_superset.py` 直接从 **git 历史**读取本 PR 的基线（ref `2ccd3f4`，即上游 main 的旧 `main.py`；仓库里不存放旧代码副本），把同一套 26 个场景分别喂给 v1.5.6 和 v2，对三家实现逐对比较"是否成功发送"：

```bash
python3 tests/test_superset.py  # 26 场景 × 3 实现 = 78 对
```

```
v1.5.6 成功 / v2 失败（回归）：0
v1.5.6 失败 / v2 成功（新增）：2（SnowLuma 的 video+兄弟段、json 空 data）
```

过程中真实抓到并修掉两个"v1.5.6 能发、v2 曾发不出"的回归点：

1. v1.5.6 **无条件发 id 节点**（即使消息没有 user_id）；v2 一度因缺 uid 跳过该节点 → 已改成 id 节点不依赖 uid；
2. 一条**只有引用、被引用消息查不到、且被引用 id 为正数**的消息：v1.5.6 在 SnowLuma 上会发成功（引用是错的），v2 一度会发失败 → 已改成这种极窄情况下退回 id 节点，与 v1.5.6 对齐（其余含引用的消息仍走内容节点用真实 `message_seq` 修正引用）。

即：**在按源码建模的规则下，v2 是 v1.5.6 的严格超集**——v1.5.6 能发成功的场景 v2 全部能发成功，另有更多场景能成功。

## 许可

GNU Affero General Public License v3.0
