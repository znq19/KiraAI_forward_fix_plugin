# Forward Fix v2.0.0 — 方案（合并转发在 NapCat / LLOneBot / SnowLuma 三家全部真实）

> 结论先行：**"id 节点 vs 内容节点"不是二选一，而是按实现分流 + 按消息逐个选最优节点 + 分级回退**。
> 三家都支持 `{"type":"node","data":{"id":...}}` 和 `{"type":"node","data":{user_id,nickname,time,content}}`，
> 但失败语义完全不同：NapCat/LLOneBot 静默跳过坏节点，SnowLuma **整条转发硬失败**。
> 所以旧版"先发 id 节点、失败再整体回退"在 SnowLuma 上会把一次成功变成一次失败+一次重试；
> v2 改为**发送前就按实现规则筛掉会让目标实现硬失败的 id 节点**。

---

## 0. 证据来源（全部为真实源码，已克隆核对）

| 实现 | 仓库/版本 | 关键文件 |
|------|-----------|----------|
| KiraAI | KiraAI-Dev/KiraAI @ v2.34.1 (`cad046f`) | `core/adapter/src/qq/qq.py`、`napcat_client/utils.py`、`core/plugin/builtin_plugins/kira-ai/tags.py`、`core/chat/message_elements.py` |
| NapCat | NapNeko/NapCatQQ @ main | `packages/napcat-onebot/action/msg/SendMsg.ts`、`action/go-cqhttp/SendForwardMsg.ts`、`api/msg.ts`、`packages/napcat-core/apis/packet.ts`、`packages/napcat-core/external/napcat.json` |
| LLOneBot | LLOneBot/LLOneBot @ main | `src/onebot11/action/go-cqhttp/SendForwardMsg.ts`、`transform/message/outgoing.ts`、`transform/message/incoming.ts`、`entities.ts`、`main/store.ts` |
| SnowLuma | SnowLuma/SnowLuma @ main | `packages/onebot/src/message-parser.ts`、`modules/message-actions.ts`、`event-converter/element-codecs.ts`、`actions/extended.ts`、`packages/core/src/bridge/apis/forward.ts`、`packages/protocol/src/element-builder.ts` |

---

## 1. 三家转发实现的事实（决定设计）

### 1.1 接口名（三家一致）
- `send_group_forward_msg` / `send_private_forward_msg`，参数 `messages`（SnowLuma/LLOneBot 也接受 `message`；NapCat 的 GoCQHTTP 变体把 `messages` 映射到 `message`）。
- 卡面自定义（三家都认，NapCat 仅 packet 模式生效）：`source` / `news:[{text}]` / `summary` / `prompt`。
- 单条转发：`forward_group_single_msg` / `forward_friend_single_msg`（三家都有）。

### 1.2 节点两种形态
```
ID 节点:      {"type":"node","data":{"id": <message_id>}}
内容节点:     {"type":"node","data":{"user_id"|"uin","nickname"|"name","time":秒,"content":[段...]}}
```
节点内嵌嵌套转发 = content 为**纯 node 数组**（三家都支持，深度上限 3）。

### 1.3 逐家差异

**NapCat**
- `id` 走 `MessageUnique.getMsgIdAndPeerByShortId(+id) || getPeerByMsgId(id)` → 用真实消息；**找不到就静默跳过该节点**（不报错）。
- `packetBackend` 默认 `"auto"`（`napcat.json`），可用时走 `uploadForwardedNodesPacket`：内容节点**保留 user_id/nickname/time**，嵌套 node 数组原生递归，id 指向转发卡片时还会 `FetchForwardMsgRaw` 把内层 protobuf piggyback → 真多层卡片。
- 非 packet 模式：内容节点会被"以机器人身份发给自己的私聊再组成转发"，**丢失原发送者身份**；id 节点仍真实。→ 这是必须优先 id 节点的原因。
- 内容节点里的 `file`/`music` 段：NapCat 会去下载 `data.url`，历史 URL 过期 → 拖垮整条转发（旧版 v1.5.4 已踩）。→ 内容回退时 NapCat 丢 file/music。
- `nickname` 在 NapCat 节点 schema 里是**必填**字符串。
- 历史接口 `get_group_msg_history` / `get_friend_msg_history` 会 `createUniqueMsgId` 注册 id → 历史 id 可作节点 id。
- reply 段：`seq` 优先（在**目标会话**里按 seq 查），否则 `id`（全局唯一表）。→ 只传 `id`，不传 `seq`（跨会话 seq 会错）。

**LLOneBot**
- `id` 走 `store.getMsgInfoByShortId(+id)`；**找不到静默丢弃**（`convertedNodes` 里 continue）。
- `OB11Entities.message()` 每次解析都会 `createMsgShortId` 并写 DB → **历史/单条/转发内消息的 id 都在库里**，id 节点稳定可用。
- 内容节点支持 `uin/user_id`、`name/nickname`、`content`、`seq`、`time`（缺失自动补）；嵌套 node 数组递归（`isInsideForward=true`）。
- reply 段只认 `id`（=shortId），查不到就跳过该段（不致命）；`seq` 字段被忽略。
- `file` 段内容节点需要可加载源：`url || file`（`uri2local`），历史 file 段的 `url` 常是 `file://<本地缓存路径>`；拿不到源会抛错。
- 转发内 `forward` 段（`data.id=resId`）会 `getForwardedMsgs` 重建 ARK → 嵌套卡片。

**SnowLuma**
- `id`/`message_id` 走 `messageStore.findEvent(id)`（SQLite 持久化，`message_id` 是 signed int32 hash，可为负）；**找不到 → 抛 INVALID_FIELD，整条转发失败**。
- `get_msg` 的实现就是 `messageStore.findEvent` → **`get_msg` 成功 ⇔ id 节点可解析**（含负 id）。
- id 节点还要求缓存事件 `user_id > 0`，且其 `message` 必须过 `assertOutboundMessageInput(..., scene='forward')`：
  - **video 不能有兄弟段**（video + 任意其他段 → 抛错）
  - poke/shake 在 forward 场景被拒
  - 未知/仅接收型段（flash_file 等）→ 抛错
- 内容节点：`user_id`/`uin` 缺省=机器人自己，`nickname` 缺省=QQ 号；`time` 为秒（uint32，毫秒/负数→用当前时间）；**content 必须全 node 或全普通段，混用抛错**；对象型元数据字段抛错；空内容抛错。
- 嵌套：content 为纯 node 数组 → 递归（深度上限 3，`MAX_FORWARD_DEPTH=3`），内层 res_id 通过 long-msg piggyback 挂到外层。
- `file` 段：内容节点里 `file_id` 或可加载 `file/url` 必需；核心转发管线会 `prepareForwardFileElement`——有 `url` 就重新上传到目标会话，有 `file_id` 且缓存同作用域就复用、否则用缓存元数据换 url 重传（**真文件转发**）。
- reply 段（**关键**）：`parseForwardNodes` 里两条路径都调 `parseMessage(content, false)`，**不传 `resolveReplySequence`**（源码 `modules/message-actions.ts:1707/1766`）。所以转发节点内的 reply 段走的是 `fromSegment` 的"直传 seq"兜底分支：`id > 0` → 当成 QQ 序列；`id <= 0` → 丢弃。
  → **SnowLuma 路径下必须注入被引用消息的真实 `message_seq`**，且含引用的消息不能走 id 节点（缓存里的引用 id 是哈希，会被误读成序列）。这正是旧版 v1.5.2「改 seq」在 NapCat 上炸、v1.5.6「全用 id」在 SnowLuma 上失效的根因。

### 1.4 KiraAI 侧（v2.34.1）
- 内置 `<forward>` → `Forward(message_id=[...], merge=bool)`；`merge="false"` = 再转发一条已有转发卡片。
- 内置发送器把 node 段塞进 `send_group_msg` → 1400（本插件要修的就是这个）。
- QQ 适配器出站只把 `Reply` 转 `{"type":"reply","data":{"id"}}` 且插到第 0 位；无 node/forward 内容节点能力。
- `after_xml_parse` 钩子拿到 `(event, actions)`，可改 `MessageChain.message_list` / pop actions —— 本插件沿用。
- 平台筛选：`event.adapter.platform`（用户配置值，QQ 适配器 manifest name = "QQ"）；sid = `adapter:gm|dm:id`。

---

## 2. 设计

### 2.1 实现探测（每 adapter 缓存，失败重探）
`get_version_info` → `data.app_name`：
- 含 `NapCat` → `napcat`
- 含 `LLOneBot` / `LLBot` → `llonebot`
- 含 `SnowLuma` → `snowluma`
- 其他/失败 → `unknown`

三家该接口返回互不混淆（NapCat.Onebot / LLOneBot / SnowLuma）。

### 2.2 消息解析（比旧版修 3 个坑）
1. **保序**：先建 `mid → msg` 映射，再按 LLM 给的顺序输出（旧版把 get_msg 补的放最后，顺序会乱）。
2. **去重**：同一 id 只留第一次。
3. **历史回退**：匹配率 <50% 时取最近 N 条**按时间升序**（旧版取 newest-first 前 N 条是倒序）。
4. 历史接口缺失（LLOneBot 无 `get_friend_msg_history`）时静默降级到逐条 `get_msg`。

### 2.3 逐条选节点（核心）
```
for (mid, msg) in resolved:
    node = pick_node(impl, mid, msg)
```
- **NAP CAT**：`id` 节点（带 user_id/nickname/time 兜底）。理由：id 路径保真度最高（文件/嵌套/媒体全部原生），且坏 id 只跳过不致命。
- **LLONEBOT**：`id` 节点（带元数据）。同上。
- **SNOWLUMA**：**内容节点优先**（id 节点路径同样要重新打包媒体，但用的是 store 旧副本：图片 URL 不刷新、引用无 resolver、嵌套无 piggyback）；内容节点构造不出时才对 id 做安全检查（因为坏 id = 整条失败）：
  - `get_msg` 成功（= store 有）且 `user_id>0`
  - content 中无「video+兄弟段」、无 poke/shake、无仅接收型段、c2c 下 file 段 ≤1
  - 通过 → `id` 节点（原生内容，媒体 URL 由 SnowLuma 自己刷新）
  - 不通过 → 退化为**净化后的内容节点**（丢掉违规段；video 只留 video；poke 丢弃；file 保留 file_id/url）
- **UNKNOWN**：id 节点 + 整体内容回退（旧行为）。

### 2.4 内容节点净化规则（回退路径 & SnowLuma 降级）
- 发送者：`user_id`（兼容 `sender.user_id`）+ `nickname`（群名片 card > nickname > QQ 号）+ `time`（秒，永远给）。
- `text/at/face/mface/json/markdown` → 原样（字段只保留标量，避免 SnowLuma `INVALID_FIELD`）。
- `image/record/video` → 有 `url/file/file_id/path` 才留；优先 `url`（新签名）→ `file` → `path`（转 `file://`）；无源丢弃。
- `file` → NapCat 内容节点**一律丢**（url 过期会拖垮整条）；LLOneBot 要有可加载源（`file://` 本地路径优先）；SnowLuma 优先 `url`（触发重传），其次 `file_id`。
- `music` → 内容节点丢弃（需签名）；id 节点原生保留。
- `reply` → `get_msg(rid)` 能解析才留 `{"type":"reply","data":{"id":rid}}`（只传 id，不传 seq）；否则按配置 `native|drop|textify`（默认 drop，绝不发坏引用）。
- `forward`（嵌套）→ `get_forward_msg(resId)` 展开成**纯 node 数组**（深度 ≤3）；展开失败则丢弃该段。
- 一条消息同时含嵌套转发和其它段时：content 用纯 node 数组（QQ 转发卡片天然独占一条消息）。

### 2.5 发送与分级回退
```
nodes = [pick_node(...)]                    # 首选
if send(nodes) ok: done
nodes2 = all_content_nodes()                # 全内容节点（净化）
if send(nodes2) ok: done
nodes3 = nodes2 去掉含 reply 的节点          # 引用兜底
if send(nodes3) ok: done
nodes4 = nodes3 去掉含嵌套转发的节点          # 嵌套兜底
if send(nodes4) ok: done
else 记日志（silent_fail 可关 → 给用户提示）
```
每次发送都带卡面 `source/summary/prompt/news`（前 4 行昵称:预览）。

### 2.6 merge=false
- 单 id → `forward_group_single_msg` / `forward_friend_single_msg`（原生再转发，保卡片原貌）。
- 失败或多 id → 走 2.5 的合并路径。

### 2.7 配置（schema）
| 键 | 类型 | 默认 | 说明 |
|----|------|------|------|
| `silent_fail` | switch | true | 失败静默 |
| `reply_mode` | enum(native/drop/textify) | native | 引用目标不可解析时：保留原样/丢弃段/文本化 |
| `prefer_content_nodes` | switch | false | 强制内容节点（调试用） |
| `max_depth` | integer | 3 | 嵌套转发展开深度上限 |
| `debug` | switch | false | 记录每个段的取舍原因 |

### 2.8 日志
每次转发打印：实现类型、解析到的 id 数/缺失数、节点形态分布（id/content）、回退到第几级、丢弃的段类型统计。

---

## 3. 验证方案（沙箱内可执行）
1. `python -m py_compile main.py`。
2. **Mock 框架**：mock `core.*`（plugin/hook/logger/message_chain/adapter_mgr/client），以包方式 import 真实 `main.py`。
3. **Mock 三家实现**（按 1.3 的规则编码，规则逐条标注源码出处）：
   - NapCat：id 未命中→跳过；内容节点保留 user_id/nickname/time；嵌套递归深度 3；file/music 在内容节点被丢。
   - LLOneBot：id 走 store（短 id，可负）；未命中→跳过；嵌套递归；reply 未命中→跳过该段。
   - SnowLuma：id 未命中→**抛错**；id 命中但 user_id<=0 或 video+兄弟段或 poke→抛错；内容节点混 node/普通段→抛错；time 毫秒→忽略；file 需 file_id/url；嵌套深度 3。
4. **用例矩阵**（3 实现 × 场景）：
   - 纯文本多条、保序、去重
   - 图片/语音/视频/表情/名片/ark
   - 文件（三种源：url / file_id / 无源）
   - 引用（可解析 / 不可解析 / 跨会话）
   - 嵌套转发（1 层 / 2 层 / 超深 / 展开失败）
   - 混合（嵌套+文本、video+文本、poke）
   - LLM 幻觉 id（匹配率 <50% → 最近 N 条升序）
   - merge=false 单条
   - id 全失败 → 内容回退；内容含 reply 失败 → 去 reply 重试
5. **断言**：目标实现收到的 payload 结构合法、节点数/顺序正确、无会让该实现硬失败的节点、SnowLuma 路径下 id 节点全部可解析。

---

## 3.5 超集回归验证（v2 ⊇ v1.5.6）
`tests/test_superset.py` 加载仓库内 v1.5.6 原始 main.py 作为基线，同一套 26 场景 × 3 实现 = 78 对逐对比较发送成功与否：
- **回归（v1.5.6 成功 → v2 失败）：0**
- 新增（v1.5.6 失败 → v2 成功）：2（SnowLuma 的 video+兄弟段、json 空 data）
- 其余 67 对双方都成功（v2 在引用/嵌套/媒体新鲜度上更正确，但那是保真度而非成败）

为保住这个超集，实现里做了三处"对齐 v1.5.6"的取舍：
1. id 节点在缺少 user_id 时**仍然发送**（v1.5.6 就是无条件发 id 节点，uid 只是兜底元数据）；
1b. 只有引用且被引用 id 为正数时允许退回 id 节点（否则 v1.5.6 能发、v2 发不出）；
2. SnowLuma 混合（转发卡片 + 兄弟段）走 id 节点——它的内容节点解析禁止 node/普通段混用，重建会丢兄弟段；
3. 只有纯文本类消息在 SnowLuma 保留 id 节点，媒体/引用/独立转发卡片才重建。

## 4. 交付物
- `main.py` / `manifest.json` / `schema.json` / `README.md` / `icon.png`
- `tests/mockenv.py` + `tests/test_matrix.py`（可直接 `python tests/test_matrix.py`）
- `PLAN.md`（本文件）
- `forward_fix_v2.0.0.zip` + sha256
