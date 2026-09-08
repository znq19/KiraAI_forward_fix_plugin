# -*- coding: utf-8 -*-
"""Demonstrate why v1.5.6's SnowLuma reply bubble is wrong even though the
send "succeeds", and how v2 fixes it.

Facts from SnowLuma's source:
  * message_id is a signed int32 hash (frequently negative) of the message.
  * message_seq is the real QQ sequence.
  * parseForwardNodes() calls parseMessage(content, false) WITHOUT a
    resolveReplySequence option, so the reply codec falls back to:
        id > 0  -> replySeq = id      (treats the OneBot id as a sequence!)
        id <= 0 -> drop the segment
"""
import copy
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mockenv  # noqa: E402
import fake_onebot as F  # noqa: E402

ROOT = os.path.dirname(HERE)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


V156 = load(os.path.join(HERE, "baseline_v1_5_6.py"), "baseline_v156").ForwardFixPlugin
V2 = load(os.path.join(ROOT, "main.py"), "forward_fix_v2").ForwardFixPlugin

BOB_SEQ = 778899


def snowluma_reply_seq(node):
    """What SnowLuma's option-less forward parser turns the reply into."""
    data = node.get("node") or {}
    if data.get("id") is not None:
        # id node: the reply lives in the stored event
        segs = (node.get("msg") or {}).get("message") or []
    else:
        segs = data.get("content") or []
    for seg in segs:
        if isinstance(seg, dict) and seg.get("type") == "reply":
            try:
                rid = int((seg.get("data") or {}).get("id"))
            except (TypeError, ValueError):
                return None
            return rid if rid > 0 else None
    return None


def build_case(bob_mid):
    bob = F.msg(bob_mid, 222, "Bob", [F.text("今晚八点开会")], t=90, seq=BOB_SEQ)
    alice = F.msg(1, 111, "Alice", [F.reply(bob_mid), F.text("收到")], t=100, seq=123)
    return {1: alice, str(bob_mid): bob}


def run(plugin_cls, messages):
    client = F.SnowLumaFake(copy.deepcopy(messages))
    ctx = mockenv.FakeCtx()
    ctx.adapter_mgr.adapters["qq"] = mockenv.FakeAdapter("qq", "QQ", client)
    plugin = plugin_cls(ctx, {"section_main": {"silent_fail": True}})
    event = mockenv.FakeEvent(sid="qq:gm:123")
    fwd = mockenv.Forward(message_id="1", merge=True)
    mockenv.run(plugin.fix_forward(event, [mockenv.MessageChain([fwd])]))
    return client


for label, bob_mid in (("Bob 的 message_id 是负数（SnowLuma 常见）", -1234567890),
                       ("Bob 的 message_id 是正数", 1234567890)):
    print("\n" + "=" * 66)
    print(label + "   (真实 QQ 序列 message_seq = %d)" % BOB_SEQ)
    print("=" * 66)
    for name, cls in (("v1.5.6", V156), ("v2.0.0", V2)):
        c = run(cls, build_case(bob_mid))
        nodes = c.accepted or []
        node = nodes[0] if nodes else None
        seq = snowluma_reply_seq(node) if node else None
        shape = node["kind"] if node else "-"
        if shape == "id":
            payload = "id=%s" % node["node"].get("id")
        elif shape == "content":
            payload = "content=%s" % [s for s in node["node"]["content"]]
        else:
            payload = "-"
        verdict = ("正确（引用到 Bob 那条消息）" if seq == BOB_SEQ
                   else ("引用丢失（气泡不显示）" if seq is None else "错误序列 %d" % seq))
        print("  %-7s 发送=%s  节点形态=%-7s %s" % (
            name, "成功" if c.accepted is not None else "失败", shape, payload))
        print("            SnowLuma 实际渲染的 replySeq = %s  ->  %s" % (seq, verdict))
