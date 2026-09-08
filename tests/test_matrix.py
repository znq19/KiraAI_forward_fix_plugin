# -*- coding: utf-8 -*-
"""Matrix tests for forward_fix v2 against the three implementation fakes."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mockenv  # noqa: E402
import fake_onebot as F  # noqa: E402

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
spec = importlib.util.spec_from_file_location("forward_fix_main", MAIN)
mod = importlib.util.module_from_spec(spec)
sys.modules["forward_fix_main"] = mod
spec.loader.exec_module(mod)
PLUGIN = mod.ForwardFixPlugin

RESULTS = []


def check(label, cond, detail=""):
    RESULTS.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label + ((" — " + str(detail)) if detail else ""))


def do(client, ids, merge=True, sid="qq:gm:123", cfg=None):
    ctx = mockenv.FakeCtx()
    ctx.adapter_mgr.adapters["qq"] = mockenv.FakeAdapter("qq", "QQ", client)
    plugin = PLUGIN(ctx, cfg or {})
    event = mockenv.FakeEvent(sid=sid)
    fwd = mockenv.Forward(message_id=",".join(str(i) for i in ids), merge=merge)
    actions = [mockenv.MessageChain([fwd])]
    mockenv.run(plugin.fix_forward(event, actions))
    return plugin, ctx, actions


def kinds(client):
    return [n["kind"] for n in (client.accepted or [])]


def accepted_ids(client):
    return [n["id"] for n in (client.accepted or []) if n["kind"] == "id"]


def content_types(node):
    return [s.get("type") for s in node.get("content") or [] if isinstance(s, dict)]


def call_params(client, action):
    for a, p in client.calls:
        if a == action:
            return p
    return None


def fresh_plugin(client, cfg=None, sid="qq:gm:123"):
    ctx = mockenv.FakeCtx()
    ctx.adapter_mgr.adapters["qq"] = mockenv.FakeAdapter("qq", "QQ", client)
    return PLUGIN(ctx, cfg or {}), ctx, sid


# ─────────────────────────────────────────────────────────── NapCat
print("\n[NapCat]")
msgs = {
    1: F.msg(1, 111, "Alice", [F.text("a1")], t=100),
    2: F.msg(2, 222, "Bob", [F.text("b2")], t=200),
    3: F.msg(3, 333, "Carol", [F.text("c3")], t=300),
}
c = F.NapCatFake(msgs)
do(c, [3, 1, 3, 2])
check("order + dedupe keeps LLM order", accepted_ids(c) == ["3", "1", "2"], accepted_ids(c))
check("id nodes carry user_id/nickname/time", all(
    all(k in n["node"] for k in ("user_id", "nickname", "time")) for n in c.accepted))
params = call_params(c, "send_group_forward_msg")
check("card meta sent", params and all(k in params for k in ("source", "summary", "prompt", "news")),
      list(params.keys()) if params else None)
check("node list sent as messages+message", params and params.get("messages") and params.get("message"))

c = F.NapCatFake({2: msgs[2]})
do(c, [2, 99])
check("unresolved id skipped (partial)", accepted_ids(c) == ["2"], accepted_ids(c))

allmsgs = {i: F.msg(i, 100 + i, "U%d" % i, [F.text(str(i))], t=i) for i in (1, 2, 3, 4, 5)}
c = F.NapCatFake(allmsgs)
do(c, [900, 901])
check("hallucination guard -> latest N ascending", accepted_ids(c) == ["4", "5"], accepted_ids(c))

media_msg = F.msg(1, 111, "Alice", [
    F.text("look"), F.image(url="http://cdn/1.jpg"), F.file_seg(url="http://cdn/f.zip"),
], t=100)
c = F.NapCatFake({1: media_msg})
c.fail_id_nodes = True
do(c, [1])
check("id-node failure -> content fallback", kinds(c) == ["content"], kinds(c))
node = c.accepted[0]["node"]
check("content fallback drops file (NapCat url download)", content_types(node) == ["text", "image"],
      content_types(node))
check("content fallback keeps url media", node["content"][1]["data"].get("url") == "http://cdn/1.jpg")
check("content node has sender metadata", all(k in node for k in ("user_id", "nickname", "time")))

nosrc = F.msg(2, 222, "Bob", [F.image(), F.text("x")], t=50)
c = F.NapCatFake({2: nosrc})
c.fail_id_nodes = True
do(c, [2])
check("sourceless media dropped", content_types(c.accepted[0]["node"]) == ["text"],
      content_types(c.accepted[0]["node"]))

c = F.NapCatFake(msgs)
do(c, [1], merge=False)
check("merge=false uses forward_group_single_msg",
      any(a == "forward_group_single_msg" for a, _ in c.calls))
c = F.NapCatFake(msgs)
c.supports_single = False
do(c, [1], merge=False)
check("single-msg unsupported -> node fallback", accepted_ids(c) == ["1"], accepted_ids(c))

c = F.NapCatFake(msgs)
do(c, [1], sid="qq:dm:999")
check("private uses send_private_forward_msg",
      any(a == "send_private_forward_msg" for a, _ in c.calls))

c = F.NapCatFake(msgs)
_, ctx, _ = do(c, [1], sid="tg:gm:1")
check("non-QQ platform ignored", c.accepted is None)

# fallback ladder: reply-carrying content nodes rejected -> no_reply variant
reply_msg = F.msg(1, 111, "Alice", [F.reply(2), F.text("hi")], t=100)
quoted = F.msg(2, 222, "Bob", [F.text("original")], t=90)
c = F.NapCatFake({1: reply_msg, 2: quoted})
c.fail_id_nodes = True
c.fail_reply_content = True
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
node = c.accepted[0]["node"] if c.accepted else {}
check("reply-rejecting target -> retried without the reply segment",
      c.accepted is not None and content_types(node) == ["text"],
      content_types(node))

# SnowLuma forward parser has no reply resolver -> a positive id is a QQ
# sequence, so the plugin must inject the quoted message's message_seq.
q = F.msg(2, 222, "Bob", [F.text("original")], t=90, seq=778899)
rm = {1: F.msg(1, 111, "Alice", [F.reply(2), F.text("quoted me")], t=100, seq=123)}
c = F.SnowLumaFake({**rm, 2: q})
do(c, [1])
node = c.accepted[0]["node"]
check("SnowLuma: reply message rebuilt as content node", kinds(c) == ["content"], (kinds(c), c.last_error))
rep = [x for x in node["content"] if x.get("type") == "reply"]
check("SnowLuma: reply id is the quoted QQ sequence",
      rep and rep[0]["data"]["id"] == "778899", rep)

# NapCat keeps the original message id (never the seq)
c = F.NapCatFake({**rm, 2: q})
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
rep = [x for x in c.accepted[0]["node"]["content"] if x.get("type") == "reply"]
check("NapCat: reply id stays the original message id",
      rep and rep[0]["data"]["id"] == "2", rep)

# SnowLuma hard-validates json (needs non-empty data) and mface (needs emoji_id)
bad_segs = {1: F.msg(1, 111, "Alice", [
    {"type": "json", "data": {"data": ""}}, {"type": "mface", "data": {"summary": "x"}},
    F.text("survivor")], t=100)}
c = F.SnowLumaFake(bad_segs)
do(c, [1])
check("SnowLuma: json without data never goes into an id node (would abort)",
      c.last_error is None and kinds(c) == ["content"]
      and content_types(c.accepted[0]["node"]) == ["text"],
      (kinds(c), c.last_error))

# market face markers survive on image segments
mf = {1: F.msg(1, 111, "Alice", [
    {"type": "image", "data": {"url": "https://gxh/x.gif", "emoji_id": "abc", "emoji_package_id": 1, "key": "k"}}],
    t=100)}
c = F.SnowLumaFake(mf)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
iseg = c.accepted[0]["node"]["content"][0]
check("SnowLuma: market-face markers kept on image segment",
      iseg["data"].get("emoji_id") == "abc", iseg)

# unknown segment type -> SnowLuma content node drops it instead of aborting
weird = {1: F.msg(1, 111, "Alice", [
    {"type": "miniapp", "data": {"data": "{}"}}, F.text("keep me")], t=100)}
c = F.SnowLumaFake(weird)
do(c, [1])
check("unknown segment type -> content node keeps the rest",
      kinds(c) == ["content"] and content_types(c.accepted[0]["node"]) == ["text"],
      (kinds(c), c.last_error))

# nested forward: NapCat/LLOneBot keep the native id node; SnowLuma rebuilds
# content with an innerForward chain (piggyback) instead of a bare resId card.
nested_store = {1: F.msg(1, 111, "Alice", [F.forward_seg("f1")], t=100)}
for cls in (F.NapCatFake, F.LLOneBotFake):
    c = cls(dict(nested_store), forwards={"f1": [F.msg(10, 111, "In", [F.text("x")], t=50)]})
    do(c, [1])
    check("%s: nested forward card keeps native id node" % cls.__name__,
          kinds(c) == ["id"], (kinds(c), c.last_error))
c = F.SnowLumaFake(dict(nested_store), forwards={"f1": [F.msg(10, 111, "In", [F.text("x")], t=50)]})
do(c, [1])
check("SnowLuma: nested forward rebuilt with innerForward content",
      kinds(c) == ["content"] and c.accepted[0]["node"]["content"][0]["kind"] == "content",
      (kinds(c), c.last_error))

# SnowLuma: a file must never carry url AND file_id (its file handler would
# take the fileId branch and can throw when the file is not cached).
c = F.SnowLumaFake({1: F.msg(1, 111, "Alice", [
    F.file_seg(url="http://cdn/a.zip", fid="fid-1")], t=100)})
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
fseg = c.accepted[0]["node"]["content"][0]
check("SnowLuma: file sends url only (no file_id alongside)",
      fseg["data"].get("url") == "http://cdn/a.zip" and "file_id" not in fseg["data"], fseg)

# unknown implementation (no get_version_info) -> id first, content fallback
c = F.NapCatFake({1: F.msg(1, 111, "Alice", [F.text("x")], t=100)})
c.version_fail = True
do(c, [1])
check("unknown impl: id node used", kinds(c) == ["id"], kinds(c))
c = F.NapCatFake({1: F.msg(1, 111, "Alice", [F.text("x")], t=100)})
c.version_fail = True
c.fail_id_nodes = True
do(c, [1])
check("unknown impl: content fallback works", kinds(c) == ["content"], kinds(c))

# ── NapCat nested forward (native id path + content fallback) ──
nested_fw = {"f1": [F.msg(10, 111, "Inner", [F.text("inner")], t=50)]}
c = F.NapCatFake({1: F.msg(1, 111, "Alice", [F.forward_seg("f1")], t=100)},
                 forwards=nested_fw)
do(c, [1])
check("NapCat: id node to a forward card is detected as nested",
      kinds(c) == ["id"] and c.accepted[0].get("nested_from_id") is True,
      c.accepted[0] if c.accepted else None)

c = F.NapCatFake({1: F.msg(1, 111, "Alice", [F.forward_seg("f1")], t=100)},
                 forwards=nested_fw)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
nested = c.accepted[0].get("nested") if c.accepted else None
check("NapCat: content fallback expands a pure node array (packet recursion)",
      kinds(c) == ["content"] and isinstance(nested, list) and len(nested) == 1,
      nested)

deep_fw = {
    "f1": [F.msg(10, 111, "L2", [F.forward_seg("f2")], t=10)],
    "f2": [F.msg(20, 222, "L3", [F.forward_seg("f3")], t=11)],
    "f3": [F.msg(30, 333, "L4", [F.forward_seg("f4")], t=12)],
    "f4": [F.msg(40, 444, "L5", [F.text("deep")], t=13)],
}
def walk_nodes(node, depth=0, out=None):
    out = out if out is not None else []
    data = node.get("data") if node.get("type") == "node" else node
    content = (data or {}).get("content") or []
    out.append((depth, content))
    for seg in content:
        if isinstance(seg, dict) and seg.get("type") == "node":
            walk_nodes(seg, depth + 1, out)
    return out

c = F.NapCatFake({1: F.msg(1, 111, "Alice", [F.forward_seg("f1")], t=100)},
                 forwards=deep_fw)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
levels = walk_nodes(c.accepted[0]["node"]) if c.accepted else []
deepest = levels[-1][1] if levels else []
check("NapCat: 4-level nesting capped at 3 cards + [聊天记录] placeholder",
      c.accepted is not None and len(levels) == 3
      and deepest == [{"type": "text", "data": {"text": "[聊天记录]"}}],
      [(d, [x.get("type") for x in content]) for d, content in levels])

# a forward card with siblings: the content node must be a PURE node array,
# otherwise NapCat's packet path drops the siblings / non-packet path skips it.
c = F.NapCatFake({1: F.msg(1, 111, "Alice", [F.text("看这个"), F.forward_seg("f1")], t=100)},
                 forwards=nested_fw)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
content = c.accepted[0]["node"]["content"] if c.accepted else []
check("NapCat: forward+siblings content is a pure node array (real nested card)",
      content and all(isinstance(x, dict) and x.get("type") == "node" for x in content)
      and c.accepted[0].get("nested") is not None,
      content)

# ─────────────────────────────────────────────────────────── LLOneBot
print("\n[LLOneBot]")
ll = {
    1: F.msg(1, 111, "Alice", [F.reply(2), F.text("quoted me"), F.file_seg(url="http://cdn/f.zip")], t=100),
    2: F.msg(2, 222, "Bob", [F.text("original")], t=90),
}
c = F.LLOneBotFake(ll)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
node = c.accepted[0]["node"]
check("content: resolvable reply kept as id only",
      any(s.get("type") == "reply" and s.get("data") == {"id": "2"} for s in node["content"]),
      node["content"])
check("content: file kept with loadable url",
      any(s.get("type") == "file" and s.get("data", {}).get("url") for s in node["content"]),
      node["content"])

bad = {3: F.msg(3, 111, "Alice", [F.reply(99), F.text("no quote")], t=10)}
c = F.LLOneBotFake(bad)
do(c, [3], cfg={"section_main": {"prefer_content_nodes": True}})
check("content: unresolvable reply dropped",
      content_types(c.accepted[0]["node"]) == ["text"],
      content_types(c.accepted[0]["node"]))

c = F.LLOneBotFake(ll)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True, "reply_mode": "textify"}})
segs = c.accepted[0]["node"]["content"]
check("content: textify renders a resolvable quote as text",
      segs and segs[0].get("type") == "text" and "[\u5f15\u7528" in segs[0]["data"]["text"],
      segs)

c = F.LLOneBotFake(ll)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True, "reply_mode": "drop"}})
check("content: reply_mode=drop removes resolvable quotes too",
      "reply" not in content_types(c.accepted[0]["node"]),
      content_types(c.accepted[0]["node"]))

c = F.LLOneBotFake({1: ll[1], 2: ll[2]})
do(c, [1], sid="qq:dm:999")
check("private chat: no friend history -> get_msg path works",
      accepted_ids(c) == ["1"] and any(a == "get_friend_msg_history" for a, _ in c.calls),
      accepted_ids(c))

# ─────────────────────────────────────────────────────────── SnowLuma
print("\n[SnowLuma]")
base = {1: F.msg(1, 111, "Alice", [F.text("hi")], t=100)}
ghost = F.msg(9, 999, "Ghost", [F.text("ghost")], t=90)
c = F.SnowLumaFake({**base, 9: ghost}, ghost_ids=[9])
do(c, [1, 9])
check("SnowLuma: ghost id rebuilt, plain text keeps id node, no hard failure",
      kinds(c) == ["id", "content"] and c.last_error is None, (kinds(c), c.last_error))

vid = {1: F.msg(1, 111, "Alice", [F.video(url="http://cdn/v.mp4"), F.text("watch")], t=100)}
c = F.SnowLumaFake(vid)
do(c, [1])
check("video+siblings -> content node keeps video only",
      kinds(c) == ["content"] and content_types(c.accepted[0]["node"]) == ["video"],
      (kinds(c), content_types(c.accepted[0]["node"])))

pk = {1: F.msg(1, 111, "Alice", [F.poke(), F.text("hi")], t=100)}
c = F.SnowLumaFake(pk)
do(c, [1])
check("poke dropped in content node",
      content_types(c.accepted[0]["node"]) == ["text"],
      content_types(c.accepted[0]["node"]))

files = {1: F.msg(1, 111, "Alice", [
    F.file_seg(url="http://cdn/f.zip"), F.file_seg(fid="fid-2"), F.file_seg(name="nosrc.bin"),
], t=100)}
c = F.SnowLumaFake(files)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
segs = c.accepted[0]["node"]["content"]
check("file: url + file_id kept, sourceless dropped",
      len(segs) == 2 and segs[0]["data"].get("url") and segs[1]["data"].get("file_id"), segs)

# c2c: at most one file per node
c = F.SnowLumaFake({1: F.msg(1, 111, "Alice", [
    F.file_seg(url="http://cdn/a.zip"), F.file_seg(fid="fid-2")], t=100)})
do(c, [1], sid="qq:dm:999", cfg={"section_main": {"prefer_content_nodes": True}})
check("c2c keeps at most one file", len(content_types(c.accepted[0]["node"])) == 1,
      content_types(c.accepted[0]["node"]))

# nested via content nodes
inner = [F.msg(10, 111, "Inner", [F.text("inner text")], t=50)]
c = F.SnowLumaFake({1: F.msg(1, 111, "Alice", [F.forward_seg("f1")], t=100)},
                   forwards={"f1": inner})
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
content = c.accepted[0]["node"]["content"]
check("nested forward expanded to node array",
      content and content[0].get("kind") == "content"
      and content[0]["node"]["content"][0]["data"]["text"] == "inner text",
      content)

# depth cap: 4 levels collapse to a placeholder instead of failing
deep_forwards = {
    "f1": [F.msg(10, 111, "L2", [F.forward_seg("f2")], t=10)],
    "f2": [F.msg(20, 222, "L3", [F.forward_seg("f3")], t=11)],
    "f3": [F.msg(30, 333, "L4", [F.forward_seg("f4")], t=12)],
    "f4": [F.msg(40, 444, "L5", [F.text("deep")], t=13)],
}
c = F.SnowLumaFake({1: F.msg(1, 111, "Alice", [F.forward_seg("f1")], t=100)},
                   forwards=deep_forwards)
do(c, [1], cfg={"section_main": {"prefer_content_nodes": True}})
check("4-level nesting capped without failure", c.last_error is None and c.accepted is not None,
      c.last_error)
lvl2 = c.accepted[0]["node"]["content"][0]["node"]
lvl3 = lvl2["content"][0]["node"]
check("deepest level uses placeholder", content_types(lvl3) == ["text"] and
      lvl3["content"][0]["data"]["text"] == "[聊天记录]", lvl3["content"])

# loss reporting: unresolved id -> logged and optionally reported to the user
c = F.NapCatFake({1: F.msg(1, 111, "Alice", [F.text("x")], t=100)})
ctx = mockenv.FakeCtx()
ctx.adapter_mgr.adapters["qq"] = mockenv.FakeAdapter("qq", "QQ", c)
plugin = PLUGIN(ctx, {"section_main": {"loss_report": True}})
ev = mockenv.FakeEvent(sid="qq:gm:123")
mockenv.run(plugin.fix_forward(ev, [mockenv.MessageChain([mockenv.Forward(message_id="1,404")])]))
check("loss_report sends a notice when an id cannot be resolved",
      any("未能转发" in "".join(getattr(e, "text", "") for e in chain.message_list)
          for _sid, chain in ctx.message_processor.sent),
      ctx.message_processor.sent)

# ─────────────────────────────────────────────────────────── shared
print("\n[shared]")
for cls in (F.NapCatFake, F.LLOneBotFake, F.SnowLumaFake):
    c = cls({1: F.msg(1, 111, "Alice", [F.text("x")], t=100)})
    do(c, [1])
    p = call_params(c, "send_group_forward_msg")
    check("%s: version probe cached" % cls.__name__,
          any(a == "get_version_info" for a, _ in c.calls))
    check("%s: card preview built" % cls.__name__,
          p and p.get("news") and p["news"][0]["text"].startswith("Alice"),
          p.get("news") if p else None)

print("\n" + "=" * 60)
passed = sum(1 for _, ok_ in RESULTS if ok_)
print("TOTAL %d/%d passed" % (passed, len(RESULTS)))
if passed != len(RESULTS):
    for label, ok_ in RESULTS:
        if not ok_:
            print("  FAILED: " + label)
    sys.exit(1)
