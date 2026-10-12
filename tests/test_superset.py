# -*- coding: utf-8 -*-
"""Differential test: v2 must forward everything v1.5.6 could forward.

Loads the pre-PR (v1.5.6) main.py straight from git history (ref 2ccd3f4, the
upstream main this PR is based on) and runs an identical scenario matrix
through both plugins against the three implementation fakes. A scenario where
v1.5.6 succeeded but v2 failed is a regression and fails the suite. Scenarios
where v1.5.6 failed and v2 succeeded are the added value.

The repo intentionally does NOT vendor a copy of the old plugin; run this
inside a git clone. Override the ref with FORWARD_FIX_BASELINE_REF.
"""
import copy
import importlib.util
import os
import subprocess
import sys
import tempfile

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


BASELINE_REF = os.environ.get("FORWARD_FIX_BASELINE_REF", "2ccd3f47cd0a")


def load_baseline():
    """Read the pre-PR main.py from git history instead of vendoring it."""
    try:
        out = subprocess.run(
            ["git", "show", "%s:main.py" % BASELINE_REF],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
    except Exception as e:  # no git, shallow clone, or ref missing
        return None, str(e).strip() or type(e).__name__
    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    tmp.write(out.stdout)
    tmp.close()
    return load(tmp.name, "baseline_v156"), None


BASE, _err = load_baseline()
if BASE is None:
    print("SKIP: v1.5.6 baseline unavailable from git (%s)" % _err)
    print("      run inside a git clone, or set FORWARD_FIX_BASELINE_REF")
    sys.exit(0)
V2MOD = load(os.path.join(ROOT, "main.py"), "forward_fix_v2")
V156, V2 = BASE.ForwardFixPlugin, V2MOD.ForwardFixPlugin


def scen(name, messages, forwards=None, ids=None, sid="qq:gm:123", merge=True, ghosts=None):
    return {
        "name": name,
        "messages": messages,
        "forwards": forwards or {},
        "ids": ids if ids is not None else list(messages.keys()),
        "sid": sid,
        "merge": merge,
        "ghosts": ghosts or [],
    }


M, T = F.msg, F.text
IMG = F.image
SCENARIOS = [
    scen("plain_text", {1: M(1, 111, "A", [T("a")], t=100),
                        2: M(2, 222, "B", [T("b")], t=200)}),
    scen("partial_unresolvable", {1: M(1, 111, "A", [T("a")], t=100)}, ids=[1, 999]),
    scen("all_unresolvable", {1: M(1, 111, "A", [T("a")], t=100),
                              2: M(2, 222, "B", [T("b")], t=200)}, ids=[901, 902]),
    scen("image_url", {1: M(1, 111, "A", [IMG(url="http://cdn/1.jpg"), T("pic")], t=100)}),
    scen("image_bare_file", {1: M(1, 111, "A", [IMG(file="abc.jpg"), T("pic")], t=100)}),
    scen("image_no_source", {1: M(1, 111, "A", [IMG(), T("pic")], t=100)}),
    scen("record_url", {1: M(1, 111, "A", [F.record(url="http://cdn/1.silk")], t=100)}),
    scen("video_url", {1: M(1, 111, "A", [F.video(url="http://cdn/1.mp4")], t=100)}),
    scen("file_url", {1: M(1, 111, "A", [F.file_seg(url="http://cdn/f.zip")], t=100)}),
    scen("file_id_only", {1: M(1, 111, "A", [F.file_seg(fid="fid-1")], t=100)}),
    scen("file_no_source", {1: M(1, 111, "A", [F.file_seg()], t=100)}),
    scen("reply_ok", {1: M(1, 111, "A", [F.reply(2), T("q")], t=100, seq=11),
                      2: M(2, 222, "B", [T("orig")], t=90, seq=22)}),
    scen("reply_bad", {1: M(1, 111, "A", [F.reply(999), T("q")], t=100, seq=11)}),
    scen("reply_only_unresolvable_pos", {1: M(1, 111, "A", [F.reply(999)], t=100, seq=11)}),
    scen("reply_only_unresolvable_neg", {1: M(1, 111, "A", [F.reply(-999)], t=100, seq=11)}),
    scen("nested_ok", {1: M(1, 111, "A", [F.forward_seg("f1")], t=100)},
         forwards={"f1": [M(10, 111, "In", [T("inner")], t=50)]}),
    scen("nested_unexpandable", {1: M(1, 111, "A", [F.forward_seg("missing")], t=100)}),
    scen("poke_only", {1: M(1, 111, "A", [F.poke()], t=100)}),
    scen("video_with_text", {1: M(1, 111, "A", [F.video(url="http://cdn/v.mp4"), T("w")], t=100)}),
    scen("json_empty", {1: M(1, 111, "A", [{"type": "json", "data": {"data": ""}}, T("x")], t=100)}),
    scen("mface_no_emoji", {1: M(1, 111, "A", [{"type": "mface", "data": {"summary": "x"}}, T("x")], t=100)}),
    scen("no_user_id", {1: M(1, 0, "?", [T("anon")], t=100)}),
    scen("ghost_id", {1: M(1, 111, "A", [T("a")], t=100),
                      9: M(9, 999, "G", [T("g")], t=90)}, ghosts=[9]),
    scen("mixed", {1: M(1, 111, "A", [
        T("look"), IMG(url="http://cdn/1.jpg"), F.file_seg(url="http://cdn/f.zip"),
        F.reply(2), F.forward_seg("f1")], t=100, seq=11),
        2: M(2, 222, "B", [T("orig")], t=90, seq=22)},
        forwards={"f1": [M(10, 111, "In", [T("inner")], t=50)]}),
    scen("private_llonebot", {1: M(1, 111, "A", [T("a")], t=100)}, sid="qq:dm:999"),
    scen("merge_false_single", {1: M(1, 111, "A", [T("a")], t=100)}, merge=False),
]


def run(plugin_cls, impl_cls, sc):
    client = impl_cls(copy.deepcopy(sc["messages"]), copy.deepcopy(sc["forwards"]),
                      ghost_ids=list(sc["ghosts"]))
    ctx = mockenv.FakeCtx()
    ctx.adapter_mgr.adapters["qq"] = mockenv.FakeAdapter("qq", "QQ", client)
    plugin = plugin_cls(ctx, {"section_main": {"silent_fail": True}})
    event = mockenv.FakeEvent(sid=sc["sid"])
    fwd = mockenv.Forward(message_id=",".join(str(i) for i in sc["ids"]), merge=sc["merge"])
    mockenv.run(plugin.fix_forward(event, [mockenv.MessageChain([fwd])]))
    return client.accepted is not None or getattr(client, "single_ok", False)


IMPLS = (F.NapCatFake, F.LLOneBotFake, F.SnowLumaFake)
regressions = []
improvements = []
rows = []

for sc in SCENARIOS:
    for impl_cls in IMPLS:
        ok156 = run(V156, impl_cls, sc)
        ok2 = run(V2, impl_cls, sc)
        tag = impl_cls.__name__.replace("Fake", "")
        rows.append((sc["name"], tag, ok156, ok2))
        if ok156 and not ok2:
            regressions.append((sc["name"], tag))
        if ok2 and not ok156:
            improvements.append((sc["name"], tag))

print("scenario                     impl        v1.5.6   v2")
for name, tag, ok156, ok2 in rows:
    print("%-28s %-11s %-8s %s" % (name, tag, "OK" if ok156 else "fail",
                                   "OK" if ok2 else "fail"))

print("\n" + "=" * 60)
print("v1.5.6 succeeded, v2 failed (REGRESSION): %d" % len(regressions))
for r in regressions:
    print("  REGRESSION: %s / %s" % r)
print("v1.5.6 failed, v2 succeeded (ADDED): %d" % len(improvements))
for r in improvements:
    print("  ADDED: %s / %s" % r)
both = sum(1 for _, _, a, b in rows if a and b)
print("both succeeded: %d / %d" % (both, len(rows)))

if regressions:
    sys.exit(1)
