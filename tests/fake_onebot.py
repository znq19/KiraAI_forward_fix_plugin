# -*- coding: utf-8 -*-
"""Fake OneBot implementations that encode the rules read from the real sources.

Sources:
  NapCat    packages/napcat-onebot/action/msg/SendMsg.ts (handleForwardedNodes*,
            node id lookup, content-node file/music behaviour), api/msg.ts
            (reply converter), action/go-cqhttp/GetGroupMsgHistory.ts.
  LLOneBot  src/onebot11/action/go-cqhttp/SendForwardMsg.ts (id -> store short id,
            skipped when missing), transform/message/outgoing.ts (reply needs a
            short id, file needs url/file, nested nodes recurse).
  SnowLuma  packages/onebot/src/modules/message-actions.ts (parseForwardNodes:
            unresolved id throws, scalar metadata, nested depth 3, mixed
            node/non-node rejected), message-parser.ts (video policy, poke
            rejected in forward, file needs file_id/url, reply best-effort).
"""


def ok(data=None):
    return {"status": "ok", "retcode": 0, "data": data if data is not None else {}}


def fail(message):
    return {"status": "failed", "retcode": 1400, "message": message}


def msg(mid, uid, nick, segs, t=1700000000, card=None, seq=None):
    m = {
        "message_id": mid,
        "user_id": uid,
        "time": t,
        "message": segs,
        "sender": {"user_id": uid, "nickname": nick},
    }
    if seq is not None:
        m["message_seq"] = seq
    if card is not None:
        m["card"] = card
    return m


def text(t):
    return {"type": "text", "data": {"text": t}}


def image(url=None, file=None, fid=None):
    d = {}
    if url:
        d["url"] = url
    if file:
        d["file"] = file
    if fid:
        d["file_id"] = fid
    return {"type": "image", "data": d}


def record(url=None, file=None):
    d = {}
    if url:
        d["url"] = url
    if file:
        d["file"] = file
    return {"type": "record", "data": d}


def video(url=None, file=None):
    return {"type": "video", "data": {k: v for k, v in (("url", url), ("file", file)) if v}}


def file_seg(url=None, fid=None, name="a.zip"):
    d = {"name": name}
    if url:
        d["url"] = url
    if fid:
        d["file_id"] = fid
    return {"type": "file", "data": d}


def reply(rid):
    return {"type": "reply", "data": {"id": str(rid)}}


def forward_seg(res_id):
    return {"type": "forward", "data": {"id": str(res_id)}}


def poke():
    return {"type": "poke", "data": {"id": "1"}}


class FakeOneBotBase:
    app_name = "Generic"
    supports_friend_history = True
    supports_single = True

    def __init__(self, messages=None, forwards=None, ghost_ids=None):
        self.store = {str(k): v for k, v in (messages or {}).items()}
        self.forwards = {str(k): v for k, v in (forwards or {}).items()}
        # ids visible in history but not resolvable via get_msg (SnowLuma store
        # miss / expired NapCat id): models the "id node would fail" case.
        self.ghost_ids = {str(x) for x in (ghost_ids or [])}
        self.calls = []
        self.accepted = None
        self.single_ok = False
        self.last_error = None
        self.fail_id_nodes = False
        self.fail_reply_content = False
        self.version_fail = False

    async def send_action(self, action, params, timeout=10.0):
        self.calls.append((action, params))
        handler = getattr(self, "a_" + action, None)
        if handler is None:
            return fail("unknown action %s" % action)
        return handler(params)

    def a_get_version_info(self, _p):
        if self.version_fail:
            return fail("unsupported action")
        return ok({"app_name": self.app_name, "app_version": "1.0.0", "protocol_version": "v11"})

    def a_get_msg(self, p):
        mid = str(p["message_id"])
        if mid in self.ghost_ids:
            return fail("消息不存在")
        m = self.store.get(mid)
        return ok(m) if m else fail("消息不存在")

    def a_get_forward_msg(self, p):
        inner = self.forwards.get(str(p.get("id")))
        return ok({"messages": inner}) if inner is not None else fail("消息已过期")

    def _history(self, p):
        msgs = sorted(self.store.values(), key=lambda m: m["time"], reverse=True)
        return ok({"messages": msgs[: int(p.get("count", 20))]})

    def a_get_group_msg_history(self, p):
        return self._history(p)

    def a_get_friend_msg_history(self, p):
        if not self.supports_friend_history:
            return fail("unsupported action")
        return self._history(p)

    def _single(self, p):
        if not self.supports_single:
            return fail("unsupported action")
        if str(p["message_id"]) in self.store:
            self.single_ok = True
            return ok({"message_id": 1})
        return fail("无法找到消息")


class NapCatFake(FakeOneBotBase):
    """packet backend default "auto": content nodes keep sender identity,
    unresolved id nodes are skipped, file/music in content nodes would
    download data.url (dropped by the plugin before sending)."""

    app_name = "NapCat.Onebot"

    def a_send_group_forward_msg(self, p):
        return self._forward(p)

    def a_send_private_forward_msg(self, p):
        return self._forward(p)

    def _forward(self, p):
        nodes = p.get("messages") or p.get("message") or []
        if not all((n or {}).get("type") == "node" for n in nodes):
            return fail("转发消息不能和普通消息混在一起发送")
        if self.fail_id_nodes and any("id" in ((n.get("data") or {})) for n in nodes):
            return fail("simulated id-node rejection")
        if self.fail_reply_content and any(
            seg.get("type") == "reply"
            for n in nodes for seg in ((n.get("data") or {}).get("content") or [])
        ):
            return fail("simulated reply-in-forward rejection")
        out = []
        for n in nodes:
            d = n.get("data") or {}
            if d.get("id") is not None:
                m = None if str(d["id"]) in self.ghost_ids else self.store.get(str(d["id"]))
                if m is None:
                    continue  # silently skipped
                # packet backend: an id node pointing at a message that itself
                # carries a forward card is detected and its inner protobuf is
                # fetched (SendMsg.ts: element.multiForwardMsgElement.resId).
                nested_from_id = any(
                    (seg or {}).get("type") == "forward"
                    for seg in (m.get("message") or [])
                )
                out.append({"kind": "id", "id": str(d["id"]), "node": d, "msg": m,
                            "nested_from_id": nested_from_id})
            else:
                out.append({"kind": "content", "node": d,
                            "nested": self._recurse(d.get("content") or [], 1)})
        if not out:
            return fail("发送合并转发消息失败：returnMsgAndResId 为空！")
        self.accepted = out
        return ok({"message_id": 1001, "res_id": "res-napcat"})

    def _recurse(self, content, dp):
        """uploadForwardedNodesPacket: pure node-array content recurses, with
        `if (dp >= 3) break`. Mixed node/non-node content is not a nested card."""
        if dp >= 3:
            return {"capped": True}
        if not content or not all((s or {}).get("type") == "node" for s in content):
            return None
        inner = []
        for n in content:
            d = n.get("data") or {}
            if d.get("id") is not None:
                m = None if str(d["id"]) in self.ghost_ids else self.store.get(str(d["id"]))
                if m is None:
                    continue
                inner.append({"kind": "id", "id": str(d["id"]), "msg": m})
            else:
                inner.append({"kind": "content",
                              "nested": self._recurse(d.get("content") or [], dp + 1)})
        return inner

    @staticmethod
    def _surviving(content):
        """Segments that survive SnowLuma's option-less parseMessage: a reply
        with a non-positive id is dropped, as is an mface without emoji_id."""
        out = []
        for seg in content:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type")
            if t == "reply":
                try:
                    rid = int((seg.get("data") or {}).get("id"))
                except (TypeError, ValueError):
                    continue
                if rid > 0:
                    out.append(seg)
                continue
            if t == "mface" and not str((seg.get("data") or {}).get("emoji_id") or "").strip():
                continue
            out.append(seg)
        return out

    def _validate_plain(self, content):
        types = [s.get("type") for s in content if isinstance(s, dict)]
        if "video" in types and len(types) > 1:
            raise AssertionError("UNSENDABLE_TYPE video with siblings")
        if any(t in ("poke", "shake") for t in types):
            raise AssertionError("UNSENDABLE_TYPE poke in forward")
        for s in content:
            t = (s or {}).get("type")
            assert t in SUPPORTED_CONTENT_TYPES, "unknown message segment type: %s" % t
            data = s.get("data") or {}
            for k, v in data.items():
                if t == "json" and k in ("data", "config"):
                    continue
                assert v is None or isinstance(v, (str, int, float, bool)), (
                    'message segment "%s" field "%s" must be a scalar value' % (t, k))
            if t == "json":
                value = data.get("data")
                assert isinstance(value, str) and value.strip(), (
                    'message segment "json" field "data" must be a JSON object or non-empty JSON string')
            if t == "file":
                fid = data.get("file_id")
                src = data.get("url") or data.get("file")
                assert fid or _loadable(src), "file segment without file_id or url"

    a_forward_group_single_msg = FakeOneBotBase._single
    a_forward_friend_single_msg = FakeOneBotBase._single


class LLOneBotFake(FakeOneBotBase):
    """id -> persistent store short id (skipped when missing); reply segments
    only accept a short id (silently dropped otherwise); no friend history."""

    app_name = "LLOneBot"
    supports_friend_history = False

    def a_send_group_forward_msg(self, p):
        return self._forward(p)

    def a_send_private_forward_msg(self, p):
        return self._forward(p)

    def _forward(self, p):
        nodes = p.get("messages") or p.get("message") or []
        out = []
        for n in nodes:
            d = n.get("data") or {}
            if d.get("id") is not None:
                m = None if str(d["id"]) in self.ghost_ids else self.store.get(str(d["id"]))
                if m is None:
                    continue  # store miss -> node dropped
                out.append({"kind": "id", "id": str(d["id"]), "node": d, "msg": m})
            else:
                content = []
                for seg in d.get("content") or []:
                    if (seg or {}).get("type") == "reply":
                        rid = str((seg.get("data") or {}).get("id") or "")
                        if rid not in self.store:
                            continue  # unresolvable reply dropped
                    content.append(seg)
                out.append({"kind": "content", "node": {**d, "content": content}})
        if not out:
            return fail("未指定消息内容")
        self.accepted = out
        return ok({"message_id": 1002, "forward_id": "res-llonebot"})

    @staticmethod
    def _surviving(content):
        """Segments that survive SnowLuma's option-less parseMessage: a reply
        with a non-positive id is dropped, as is an mface without emoji_id."""
        out = []
        for seg in content:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type")
            if t == "reply":
                try:
                    rid = int((seg.get("data") or {}).get("id"))
                except (TypeError, ValueError):
                    continue
                if rid > 0:
                    out.append(seg)
                continue
            if t == "mface" and not str((seg.get("data") or {}).get("emoji_id") or "").strip():
                continue
            out.append(seg)
        return out

    def _validate_plain(self, content):
        types = [s.get("type") for s in content if isinstance(s, dict)]
        if "video" in types and len(types) > 1:
            raise AssertionError("UNSENDABLE_TYPE video with siblings")
        if any(t in ("poke", "shake") for t in types):
            raise AssertionError("UNSENDABLE_TYPE poke in forward")
        for s in content:
            t = (s or {}).get("type")
            assert t in SUPPORTED_CONTENT_TYPES, "unknown message segment type: %s" % t
            data = s.get("data") or {}
            for k, v in data.items():
                if t == "json" and k in ("data", "config"):
                    continue
                assert v is None or isinstance(v, (str, int, float, bool)), (
                    'message segment "%s" field "%s" must be a scalar value' % (t, k))
            if t == "json":
                value = data.get("data")
                assert isinstance(value, str) and value.strip(), (
                    'message segment "json" field "data" must be a JSON object or non-empty JSON string')
            if t == "file":
                fid = data.get("file_id")
                src = data.get("url") or data.get("file")
                assert fid or _loadable(src), "file segment without file_id or url"

    a_forward_group_single_msg = FakeOneBotBase._single
    a_forward_friend_single_msg = FakeOneBotBase._single


SUPPORTED_CONTENT_TYPES = {
    "text", "at", "face", "mface", "image", "record", "video", "file",
    "json", "markdown", "xml", "reply", "forward", "contact", "dice", "rps",
}


def _loadable(src):
    if not src:
        return False
    if src.startswith(("http://", "https://", "file://", "base64://", "data:")):
        return True
    return "/" in src or "\\" in src


class SnowLumaFake(FakeOneBotBase):
    """Strict: an unresolved id node aborts the whole forward; the cached event
    must pass forward-scene validation; content must be all-node or all-plain;
    metadata must be scalar; nested depth <= 3; file needs file_id/url."""

    app_name = "SnowLuma"
    MAX_DEPTH = 3

    def a_send_group_forward_msg(self, p):
        return self._forward(p, is_group=True)

    def a_send_private_forward_msg(self, p):
        return self._forward(p, is_group=False)

    def _forward(self, p, is_group):
        nodes = p.get("messages") or p.get("message") or []
        if not isinstance(nodes, list) or not nodes:
            return fail("forward messages must contain at least one node")
        try:
            out = [self._parse_node(n, 0, is_group) for n in nodes]
        except AssertionError as e:
            self.last_error = str(e)
            return fail(str(e))
        self.accepted = out
        return ok({"message_id": 1003, "res_id": "res-snowluma"})

    def _parse_node(self, seg, depth, is_group):
        if depth >= self.MAX_DEPTH:
            raise AssertionError("forward nesting depth exceeds 3")
        assert (seg or {}).get("type") == "node", "unknown forward message segment type"
        d = seg.get("data")
        assert isinstance(d, dict), "forward messages[i].data must be an object"
        for k, v in d.items():
            if k in ("content", "message"):
                continue
            assert v is None or isinstance(v, (str, int, float, bool)), (
                "forward messages[i].%s must be a scalar value" % k)

        if d.get("id") is not None:
            m = None if str(d["id"]) in self.ghost_ids else self.store.get(str(d["id"]))
            if m is None:
                raise AssertionError("forward node message_id not found: %s" % d["id"])
            uid = m.get("user_id")
            assert uid and int(uid) > 0, (
                "forward node message_id %s has no valid sender user_id" % d["id"])
            # The real parser runs parseMessage() over the stored event, so the
            # same hard validations apply as for a content node.
            self._validate_plain(m.get("message") or [])
            if not self._surviving(m.get("message") or []):
                raise AssertionError("forward node content is empty")
            return {"kind": "id", "id": str(d["id"]), "node": d, "msg": m}

        content = d.get("content") or d.get("message") or []
        assert isinstance(content, list), "content must be a list"
        if content and all((s or {}).get("type") == "node" for s in content):
            inner = [self._parse_node(s, depth + 1, is_group) for s in content]
            return {"kind": "content", "node": {**d, "content": inner}}

        self._validate_plain(content)
        if not content or not self._surviving(content):
            raise AssertionError("forward node content is empty")
        if not is_group and sum(1 for s in content if s.get("type") == "file") > 1:
            raise AssertionError("a private forward node can contain at most one file element")
        return {"kind": "content", "node": {**d, "content": content}}

    @staticmethod
    def _surviving(content):
        """Segments that survive SnowLuma's option-less parseMessage: a reply
        with a non-positive id is dropped, as is an mface without emoji_id."""
        out = []
        for seg in content:
            if not isinstance(seg, dict):
                continue
            t = seg.get("type")
            if t == "reply":
                try:
                    rid = int((seg.get("data") or {}).get("id"))
                except (TypeError, ValueError):
                    continue
                if rid > 0:
                    out.append(seg)
                continue
            if t == "mface" and not str((seg.get("data") or {}).get("emoji_id") or "").strip():
                continue
            out.append(seg)
        return out

    def _validate_plain(self, content):
        types = [s.get("type") for s in content if isinstance(s, dict)]
        if "video" in types and len(types) > 1:
            raise AssertionError("UNSENDABLE_TYPE video with siblings")
        if any(t in ("poke", "shake") for t in types):
            raise AssertionError("UNSENDABLE_TYPE poke in forward")
        for s in content:
            t = (s or {}).get("type")
            assert t in SUPPORTED_CONTENT_TYPES, "unknown message segment type: %s" % t
            data = s.get("data") or {}
            for k, v in data.items():
                if t == "json" and k in ("data", "config"):
                    continue
                assert v is None or isinstance(v, (str, int, float, bool)), (
                    'message segment "%s" field "%s" must be a scalar value' % (t, k))
            if t == "json":
                value = data.get("data")
                assert isinstance(value, str) and value.strip(), (
                    'message segment "json" field "data" must be a JSON object or non-empty JSON string')
            if t == "file":
                fid = data.get("file_id")
                src = data.get("url") or data.get("file")
                assert fid or _loadable(src), "file segment without file_id or url"

    a_forward_group_single_msg = FakeOneBotBase._single
    a_forward_friend_single_msg = FakeOneBotBase._single
