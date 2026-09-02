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
- ✅ **媒体真实转发**：通过消息 ID 引用，QQ 客户端自动拉取原消息完整内容（图片 / 视频 / 文件等）
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
  2. 直接调用 OneBot 专用接口：
     - 群聊 → send_group_forward_msg
     - 私聊 → send_private_forward_msg
        ↓
OneBot 正常发送合并转发 ✅
```

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
