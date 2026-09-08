"""
forward_fix — make KiraAI's <forward> merged forward real on NapCat / LLOneBot / SnowLuma.

Why this plugin exists
----------------------
KiraAI's built-in ForwardTag turns <forward merge="true">id1,id2</forward> into a
`Forward` element, and QQAdapter._send_forward then sends `{"type":"node"}` segments
through send_group_msg / send_direct_msg. OneBot rejects node segments outside a
forward node list with retcode 1400. This plugin intercepts the `after_xml_parse`
stage and calls the dedicated OneBot APIs instead.

Design (verified against the three implementations' sources)
------------------------------------------------------------
Every implementation accepts a node list on send_group_forward_msg /
send_private_forward_msg. There are two node shapes:

  ID node       {"type":"node","data":{"id": <message_id>}}
  content node  {"type":"node","data":{"user_id"|"uin","nickname"|"name",
                                       "time":<unix seconds>,"content":[segments]}}

Nested forward inside a node = `content` is a *pure* array of node objects
(all three implementations support this, depth limit 3).

* NapCat  — id resolves through MessageUnique (short id or raw msgId); an
  unresolved id is skipped silently. The packet backend (default "auto" in
  napcat.json) preserves user_id/nickname/time for content nodes and supports
  nested node arrays. In non-packet mode content nodes are re-sent by the bot
  itself, so the original sender identity is LOST -> id nodes are strictly
  better. Content-node file/music segments make NapCat download data.url and an
  expired history url fails the whole forward -> drop them.
* LLOneBot — id resolves through its persistent store; every message it parses
  (events, history, get_forward_msg) registers a short id there, so id nodes are
  stable. Unresolved ids are skipped. reply segments only accept a short id.
* SnowLuma — id resolves through its SQLite message store; an unresolved id
  ABORTS the whole forward (INVALID_FIELD). Its cached event must also pass the
  forward-scene validation: user_id > 0, no video with sibling segments, no
  poke/shake, no receive-only segment types, at most one file per c2c node.
  get_msg and the id-node lookup hit the same store, so `get_msg(id)` success is
  a reliable pre-flight check.

Therefore: pick the highest-fidelity node per message, but never hand SnowLuma
an id it cannot resolve. Falls back through content-node variants that drop the
segments a target implementation refuses, instead of failing the whole forward.
"""

import logging
import os
import time

from core.plugin import BasePlugin, on, Priority
from core.chat import MessageChain
from core.chat.message_elements import Forward, Text
from core.tag import RootTagAction

logger = logging.getLogger(__name__)

# KiraAI sid format: <adapter>:<dm|gm>:<id> (core/chat/message_utils.py).
_GROUP_SESSION_TYPES = {"gm"}
_PRIVATE_SESSION_TYPES = {"dm"}

# Implementation ids resolved from get_version_info.data.app_name.
_IMPL_NAPCAT = "napcat"
_IMPL_LLONEBOT = "llonebot"
_IMPL_SNOWLUMA = "snowluma"
_IMPL_UNKNOWN = "unknown"

# SnowLuma rejects these in a forward scene (assertOutboundMessageInput).
_SNOWLUMA_FORBIDDEN_TYPES = {"poke", "shake"}
_SNOWLUMA_RECEIVE_ONLY = {"flashtransfer", "onlinefile", "flash_file"}
# Segment types SnowLuma's outbound parser understands. An id node whose
# cached message carries anything else would abort the whole forward.
_SNOWLUMA_KNOWN_TYPES = {
    "text", "at", "face", "mface", "image", "record", "video", "file",
    "json", "markdown", "xml", "forward", "reply", "dice", "rps",
    "contact", "music", "share", "location",
}

# Segments that are safely copyable into a content node (scalar data only).
_COPYABLE_TYPES = {
    "text", "at", "face", "mface", "json", "markdown", "xml",
    "contact", "dice", "rps",
}
_MEDIA_TYPES = {"image", "record", "video"}

# Fields kept per copyable segment type. Everything else is dropped so a
# target implementation's strict validator (SnowLuma assertScalarSegmentData)
# never sees object-valued metadata.
_SEGMENT_FIELDS = {
    "text": ("text",),
    "at": ("qq", "name", "nickname", "card", "uid"),
    "face": ("id", "resultId", "chainCount"),
    "mface": ("emoji_package_id", "emoji_id", "key", "summary"),
    "json": ("data", "config"),
    "markdown": ("content",),
    "xml": ("data",),
    "contact": ("type", "id"),
    "dice": ("result",),
    "rps": ("result",),
}

# Short previews for the forward card bubble (NapCat/LLOneBot/SnowLuma all
# render up to 4 "nickname: preview" lines).
_PREVIEW = {
    "text": None, "at": "@", "face": "[表情]", "mface": "[表情]",
    "image": "[图片]", "record": "[语音]", "video": "[视频]", "file": "[文件]",
    "json": "[JSON消息]", "markdown": "[Markdown]", "xml": "[XML消息]",
    "forward": "[聊天记录]", "reply": "", "poke": "[戳一戳]",
    "dice": "[骰子]", "rps": "[猜拳]", "contact": "[推荐]",
}


def sid_of(adapter_name: str, is_group: bool, session_id: str) -> str:
    return "%s:%s:%s" % (adapter_name, "gm" if is_group else "dm", session_id)


class ForwardFixPlugin(BasePlugin):
    """Transparently fix KiraAI's built-in <forward> merge-forward feature."""

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        sec = cfg.get("section_main", {})
        self.silent_fail = sec.get("silent_fail", True)
        reply_mode = str(sec.get("reply_mode", "native") or "native").strip().lower()
        self.reply_mode = reply_mode if reply_mode in ("native", "drop", "textify") else "native"
        self.prefer_content = bool(sec.get("prefer_content_nodes", False))
        try:
            self.max_depth = int(sec.get("max_depth", 3))
        except (TypeError, ValueError):
            self.max_depth = 3
        # All three implementations cap nesting at 3 levels.
        self.max_depth = max(0, min(3, self.max_depth))
        self.debug = bool(sec.get("debug", False))
        # loss_report: send a short notice listing anything that could not be
        # forwarded; debug_dump: log the exact node payload sent.
        self.loss_report = bool(sec.get("loss_report", False))
        self.debug_dump = bool(sec.get("debug_dump", False))

        # Per-adapter implementation cache: adapter_name -> impl id.
        self._impl_cache: dict[str, str] = {}
        # Probe caches keyed by (id(client), message_id) so concurrent
        # forwards on different sessions never invalidate each other.
        self._msg_cache: dict[tuple, dict] = {}
        self._reply_ok_cache: dict[tuple, bool] = {}
        self._reply_text_cache: dict[tuple, str] = {}
        self._reply_seq_cache: dict[tuple, int] = {}
        self._cache_cap = 4096

    async def initialize(self):
        logger.info("[forward_fix] ready (v2.0.0)")

    async def terminate(self):
        # Nothing persistent to tear down; keep it re-entrant.
        self._impl_cache.clear()
        self._msg_cache.clear()

    # ------------------------------------------------------------------ hook

    @on.after_xml_parse(priority=Priority.HIGH)
    async def fix_forward(self, event, actions: list, *_):
        if getattr(event, "adapter", None) is None:
            return
        platform = getattr(event.adapter, "platform", "") or ""
        if platform.lower() != "qq":
            return

        sid = getattr(event, "sid", None) or getattr(
            getattr(event, "session", None), "sid", None
        )
        if not sid:
            return
        parts = sid.split(":", 2)
        if len(parts) != 3:
            return
        adapter_name, session_type, session_id = parts

        is_group = session_type in _GROUP_SESSION_TYPES
        if not is_group and session_type not in _PRIVATE_SESSION_TYPES:
            return

        for i in range(len(actions) - 1, -1, -1):
            action = actions[i]

            # Defensive: a root <forward> tag (the built-in tag uses parent="msg").
            if isinstance(action, RootTagAction):
                if getattr(action.tag, "name", None) == "forward":
                    ids = self._parse_ids(action.value)
                    if ids:
                        await self._send_forward(
                            adapter_name, is_group, session_id, ids, True
                        )
                    actions.pop(i)
                continue

            if not isinstance(action, MessageChain):
                continue

            new_list = []
            forwards = []  # (ids, merge)
            for elem in action.message_list:
                if isinstance(elem, Forward):
                    ids = self._parse_ids(elem.message_id)
                    if ids:
                        forwards.append((ids, getattr(elem, "merge", True)))
                    else:
                        # Unparsable ids: keep the element so the built-in
                        # sender reports the failure instead of swallowing it.
                        logger.warning(
                            "[forward_fix] Forward with unparsable ids left to "
                            "the built-in sender: %r", elem.message_id
                        )
                        new_list.append(elem)
                else:
                    new_list.append(elem)

            if not forwards:
                continue

            # Remove the Forward elements we are about to handle ourselves.
            action.message_list = new_list
            if not new_list:
                actions.pop(i)

            for ids, merge in forwards:
                ok = await self._send_forward(
                    adapter_name, is_group, session_id, ids, merge
                )
                if ok:
                    logger.info(
                        "[forward_fix] forwarded %d message(s) to %s:%s",
                        len(ids), session_type, session_id,
                    )
                else:
                    logger.error(
                        "[forward_fix] forward FAILED (%d message(s), %s:%s)",
                        len(ids), session_type, session_id,
                    )
                    if not self.silent_fail:
                        await self._notify_failure(sid)

    # ----------------------------------------------------------- send ladder

    async def _send_forward(
        self, adapter_name: str, is_group: bool, session_id: str,
        message_ids: list[int], merge: bool,
    ) -> bool:
        """Send one merged forward; returns True when a node list was accepted."""
        try:
            adapter = self.ctx.adapter_mgr.get_adapter(adapter_name)
            if adapter is None:
                logger.error("[forward_fix] adapter %r not found", adapter_name)
                return False
            client = adapter.get_client()
            if client is None:
                logger.error("[forward_fix] adapter %r has no client", adapter_name)
                return False

            impl = await self._detect_impl(client, adapter_name)
            logger.info(
                "[forward_fix] target=%s ids=%s merge=%s",
                impl, message_ids, merge,
            )

            # merge="false" + a single id: re-forward the original card natively
            # (forward_group_single_msg / forward_friend_single_msg). Falls back
            # to the node path when the target cannot re-forward that message.
            if not merge and len(message_ids) == 1:
                single_ok = True
                if impl == _IMPL_SNOWLUMA:
                    probe = await self._probe_msg(client, message_ids[0])
                    types = [s.get("type") for s in (self._segments(probe) or [])] if probe else []
                    # SnowLuma's forwardSingleMessage also parses without reply
                    # resolvers; rebuild such messages through the node path.
                    single_ok = "reply" not in types
                if single_ok and await self._forward_single(
                    client, is_group, session_id, message_ids[0]
                ):
                    return True
                logger.warning(
                    "[forward_fix] single-message re-forward skipped/failed; "
                    "using a node-based forward"
                )

            loss: list[dict] = []
            resolved = await self._resolve_messages(
                client, is_group, session_id, message_ids, loss
            )
            if not resolved:
                logger.error("[forward_fix] no resolvable messages for %s", message_ids)
                return False

            # Phase 1: per-message best node (id-first, SnowLuma pre-flight).
            nodes = []
            all_content = True
            for mid, msg in resolved:
                node = await self._pick_node(impl, client, mid, msg, is_group, 0, loss)
                if node is None:
                    continue
                if (node.get("data") or {}).get("id") is not None:
                    all_content = False
                nodes.append(node)
            if nodes and await self._send_nodes(client, is_group, session_id, nodes, resolved):
                await self._report_loss(sid_of(adapter_name, is_group, session_id), loss)
                return True

            # Phases 2-4: rebuild everything as content nodes, progressively
            # dropping the parts a target implementation may refuse.
            variants = ["no_reply", "no_nested"] if all_content else ["full", "no_reply", "no_nested"]
            for variant in variants:
                nodes = await self._build_all_content(
                    impl, client, resolved, is_group, variant, loss
                )
                if not nodes:
                    continue
                if await self._send_nodes(client, is_group, session_id, nodes, resolved):
                    await self._report_loss(sid_of(adapter_name, is_group, session_id), loss)
                    return True
                logger.warning(
                    "[forward_fix] content-node variant %r rejected; trying next",
                    variant,
                )
            return False
        except Exception as e:
            logger.error("[forward_fix] forward raised: %s", e, exc_info=self.debug)
            return False

    async def _build_all_content(self, impl, client, resolved, is_group, variant, loss=None):
        nodes = []
        for _mid, msg in resolved:
            node = await self._build_content_node(
                client, msg, is_group, impl, 0,
                include_reply=(variant != "no_reply"),
                include_nested=(variant != "no_nested"),
                loss=loss, mid=_mid,
            )
            if node:
                nodes.append(node)
        return nodes

    # --------------------------------------------------------- implementation

    async def _detect_impl(self, client, adapter_name: str) -> str:
        cached = self._impl_cache.get(adapter_name)
        if cached:
            return cached
        impl = _IMPL_UNKNOWN
        try:
            resp = await client.send_action("get_version_info", {}, timeout=10)
            if isinstance(resp, dict) and resp.get("status") == "ok":
                name = str((resp.get("data") or {}).get("app_name") or "").lower()
                if "napcat" in name:
                    impl = _IMPL_NAPCAT
                elif "llonebot" in name or "llbot" in name:
                    impl = _IMPL_LLONEBOT
                elif "snowluma" in name:
                    impl = _IMPL_SNOWLUMA
                else:
                    logger.info("[forward_fix] unknown OneBot app_name=%r", name)
            else:
                logger.info("[forward_fix] get_version_info returned %r", resp)
        except Exception as e:
            # Transient (not logged in yet): do not cache, probe again next time.
            logger.warning("[forward_fix] get_version_info failed: %s", e)
            return impl
        self._impl_cache[adapter_name] = impl
        return impl

    # ------------------------------------------------------------- resolution

    @staticmethod
    def _parse_ids(raw) -> list[int] | None:
        if not raw:
            return None
        if isinstance(raw, str):
            tokens = [x.strip() for x in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            tokens = [str(x).strip() for x in raw]
        else:
            tokens = [str(raw).strip()]
        ids: list[int] = []
        seen = set()
        for token in tokens:
            if not token:
                continue
            try:
                mid = int(token)
            except (TypeError, ValueError):
                continue
            if mid not in seen:
                seen.add(mid)
                ids.append(mid)
        return ids or None

    async def _resolve_messages(self, client, is_group: bool, session_id: str,
                                message_ids: list[int], loss: list | None = None
                                ) -> list[tuple[str, dict]]:
        """Return [(str(id), message_dict)] in the LLM's order, deduplicated."""
        history = await self._fetch_history(
            client, is_group, session_id, max(len(message_ids) * 2, 20)
        )
        by_id: dict[str, dict] = {}
        for m in history:
            mid = m.get("message_id")
            if mid is not None:
                by_id.setdefault(str(mid), m)

        resolved: list[tuple[str, dict]] = []
        seen: set[str] = set()
        matched = 0
        for mid in message_ids:
            key = str(mid)
            if key in seen:
                continue
            seen.add(key)
            msg = by_id.get(key)
            if msg is None:
                msg = await self._probe_msg(client, mid)
            if msg is None:
                logger.warning("[forward_fix] message_id %s not found; skipped", mid)
                if loss is not None:
                    loss.append({"mid": key, "type": "message", "reason": "unresolved id"})
                continue
            resolved.append((key, msg))
            matched += 1

        # Hallucination guard: if most ids cannot be resolved, trust the latest
        # N real history messages instead (chronological order).
        if matched < len(message_ids) * 0.5:
            logger.warning(
                "[forward_fix] only %d/%d ids resolved; using latest %d history messages",
                matched, len(message_ids), len(message_ids),
            )
            fallback = self._latest_history(history, len(message_ids))
            if fallback:
                return fallback
        return resolved

    @staticmethod
    def _latest_history(history: list[dict], n: int) -> list[tuple[str, dict]]:
        usable = [m for m in history if m.get("message_id") is not None]
        if not usable:
            return []
        if any(m.get("time") for m in usable):
            usable = sorted(usable, key=lambda m: m.get("time") or 0)
        else:
            usable = list(reversed(usable))  # most implementations return newest first
        return [(str(m["message_id"]), m) for m in usable[-n:]]

    async def _fetch_history(self, client, is_group: bool, session_id: str,
                             count: int) -> list[dict]:
        try:
            if is_group:
                resp = await client.send_action(
                    "get_group_msg_history",
                    {"group_id": int(session_id), "count": count},
                    timeout=15,
                )
            else:
                resp = await client.send_action(
                    "get_friend_msg_history",
                    {"user_id": int(session_id), "count": count},
                    timeout=15,
                )
            if isinstance(resp, dict) and resp.get("status") == "ok":
                return (resp.get("data") or {}).get("messages") or []
        except Exception as e:
            # LLOneBot has no get_friend_msg_history; degrade to per-id get_msg.
            logger.debug("[forward_fix] history fetch failed: %s", e)
        return []

    async def _probe_msg(self, client, mid) -> dict | None:
        key = (id(client), str(mid))
        if key in self._msg_cache:
            return self._msg_cache[key]
        if len(self._msg_cache) > self._cache_cap:
            self._msg_cache.clear()
        result = None
        try:
            resp = await client.send_action("get_msg", {"message_id": mid}, timeout=15)
            if isinstance(resp, dict) and resp.get("status") == "ok":
                data = resp.get("data") or {}
                if data.get("message") is not None:
                    result = data
        except Exception as e:
            logger.debug("[forward_fix] get_msg(%s) failed: %s", mid, e)
        self._msg_cache[key] = result
        return result

    async def _fetch_forward(self, client, res_id: str) -> list | None:
        try:
            resp = await client.send_action("get_forward_msg", {"id": str(res_id)}, timeout=15)
            if isinstance(resp, dict) and resp.get("status") == "ok":
                return (resp.get("data") or {}).get("messages") or []
        except Exception as e:
            logger.debug("[forward_fix] get_forward_msg(%s) failed: %s", res_id, e)
        return None

    async def _reply_resolvable(self, client, rid: str) -> bool:
        key = (id(client), str(rid))
        if key in self._reply_ok_cache:
            return self._reply_ok_cache[key]
        try:
            rid_int = int(rid)
        except (TypeError, ValueError):
            self._reply_ok_cache[key] = False
            return False
        msg = await self._probe_msg(client, rid_int)
        ok = msg is not None and msg.get("message") is not None
        self._reply_ok_cache[key] = ok
        return ok

    async def _reply_seq(self, client, rid: str) -> int:
        """QQ sequence of the quoted message (SnowLuma's forward parser reads a
        positive reply id as a sequence because parseForwardNodes calls
        parseMessage without resolveReplySequence)."""
        key = (id(client), str(rid))
        if key in self._reply_seq_cache:
            return self._reply_seq_cache[key]
        seq = 0
        try:
            msg = await self._probe_msg(client, int(rid))
        except (TypeError, ValueError):
            msg = None
        if msg:
            try:
                seq = int(msg.get("message_seq") or 0)
            except (TypeError, ValueError):
                seq = 0
        if seq <= 0:
            seq = 0
        self._reply_seq_cache[key] = seq
        return seq

    async def _reply_text(self, client, rid: str) -> str:
        key = (id(client), str(rid))
        if key in self._reply_text_cache:
            return self._reply_text_cache[key]
        text = ""
        try:
            msg = await self._probe_msg(client, int(rid))
        except (TypeError, ValueError):
            msg = None
        if msg:
            uid, nick = self._sender_info(msg)
            body = self._preview_of(msg)
            text = f"[引用 {nick or uid or rid}: {body}]" if (nick or uid) else f"[引用 {rid}]"
        self._reply_text_cache[key] = text
        return text

    # ------------------------------------------------------------ node choice

    async def _pick_node(self, impl, client, mid: str, msg: dict,
                         is_group: bool, depth: int, loss: list | None = None):
        if self.prefer_content:
            return await self._build_content_node(client, msg, is_group, impl, depth,
                                                  loss=loss, mid=mid)
        if impl == _IMPL_SNOWLUMA:
            # SnowLuma's id-node path rebuilds every element from the STORED
            # event: stale image urls, no reply resolver (a positive id is read
            # as a QQ sequence) and no nested-forward piggyback. So rebuild
            # ourselves whenever media / a quote / a nested forward is present,
            # and keep the id node for plain text (verbatim stored sender).
            types = [s.get("type") for s in (self._segments(msg) or [])]
            has_reply = "reply" in types
            has_forward = "forward" in types
            has_media = any(t in ("image", "record", "video", "file") for t in types)
            # A quote must be rebuilt (id nodes misread the hash as a sequence);
            # media must be rebuilt (id nodes reuse the stored, possibly stale
            # url); a lone forward card must be rebuilt to get the piggyback.
            # A forward mixed with siblings must NOT be rebuilt: SnowLuma
            # forbids mixing node/non-node content, so the id node is the only
            # shape that keeps both the card and the siblings.
            needs_rebuild = has_reply or has_media or (has_forward and len(types) == 1)
            if not needs_rebuild and await self._snowluma_id_ok(client, mid, msg, is_group):
                return self._build_id_node(msg, mid)
            node = await self._build_content_node(client, msg, is_group, impl, depth,
                                                  loss=loss, mid=mid)
            if node is not None:
                return node
            # Nothing reconstructable: an id node is still worth trying, but
            # only when its pre-flight says SnowLuma will not abort.
            if await self._snowluma_id_ok(client, mid, msg, is_group):
                return self._build_id_node(msg, mid)
            return None
        # NapCat / LLOneBot / unknown: id nodes reuse the original message
        # (real media, files, nested cards and reply bubbles).
        return self._build_id_node(msg, mid)

    def _build_id_node(self, msg: dict, mid: str):
        # NapCat/LLOneBot resolve the id themselves and ignore these fields;
        # they are only a fallback for implementations that prefer content.
        # Never skip an id node just because the sender is unknown (v1.5.6
        # parity): SnowLuma's pre-flight rejects user_id<=0 before we get here.
        uid, nick = self._sender_info(msg)
        data = {
            "id": str(mid),
            "nickname": str(nick or uid or "QQ用户"),
            "time": self._msg_time(msg),
        }
        if uid:
            data["user_id"] = str(uid)
        return {"type": "node", "data": data}

    async def _snowluma_id_ok(self, client, mid: str, msg: dict, is_group: bool) -> bool:
        """True when SnowLuma's id-node path will not abort the forward."""
        probe = await self._probe_msg(client, mid)
        if probe is None:
            return False
        uid, _ = self._sender_info(probe)
        try:
            if not uid or int(uid) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        segs = self._segments(probe)
        if segs is None:
            return False
        types = [s.get("type") for s in segs if isinstance(s, dict)]
        if not types:
            return False
        if "video" in types and len(types) > 1:
            return False
        if any(t in _SNOWLUMA_FORBIDDEN_TYPES for t in types):
            return False
        if any(t in _SNOWLUMA_RECEIVE_ONLY for t in types):
            return False
        if any(t not in _SNOWLUMA_KNOWN_TYPES for t in types):
            return False
        for seg in segs:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "json":
                value = (seg.get("data") or {}).get("data")
                # SnowLuma's json codec throws on empty/missing data, which
                # aborts the whole id-node forward.
                if value is None or (isinstance(value, str) and not value.strip()):
                    return False
        if not is_group and types.count("file") > 1:
            return False
        # A cached reply segment carries the message-id hash, which SnowLuma's
        # option-less forward parser would misread as a QQ sequence. Rebuild
        # this node as content instead, where the real sequence is injected.
        # Only exception: a message whose *only* content is a reply whose raw
        # id is positive — then the id path still yields a (wrong but non-empty)
        # quote instead of aborting, so v1.5.6 could send it and we keep that.
        if "reply" in types:
            non_reply = [t for t in types if t != "reply"]
            raw_positive = False
            for seg in segs:
                if isinstance(seg, dict) and seg.get("type") == "reply":
                    try:
                        if int((seg.get("data") or {}).get("id")) > 0:
                            raw_positive = True
                            break
                    except (TypeError, ValueError):
                        pass
            if non_reply or not raw_positive:
                return False
        return True

    # --------------------------------------------------------- content nodes

    async def _build_content_node(self, client, msg: dict, is_group: bool, impl: str,
                                  depth: int = 0, include_reply: bool = True,
                                  include_nested: bool = True, loss: list | None = None,
                                  mid: str = ""):
        def note(stype, reason):
            dropped[stype] = dropped.get(stype, 0) + 1
            if loss is not None:
                loss.append({"mid": str(mid), "type": stype, "reason": reason})

        segs = self._segments(msg)
        uid, nick = self._sender_info(msg)
        if segs is None or not uid:
            return None

        usable: list[dict] = []
        nested: list[dict] | None = None
        dropped: dict[str, int] = {}
        has_video = False
        depth_capped = False

        for seg in segs:
            stype = seg.get("type")
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}

            if stype in _COPYABLE_TYPES:
                norm = self._scalar_data(stype, data)
                if not self._segment_safe(stype, norm):
                    note(stype, "fails target validation")
                    continue
                if norm:
                    usable.append({"type": stype, "data": norm})
                continue

            if stype in _MEDIA_TYPES:
                norm = self._media_data(stype, data)
                if norm:
                    usable.append({"type": stype, "data": norm})
                    has_video = has_video or stype == "video"
                else:
                    note(stype, "no loadable media source")
                continue

            if stype == "file":
                norm = self._file_data(data, impl)
                if norm:
                    usable.append({"type": "file", "data": norm})
                else:
                    note("file", "no loadable file source")
                continue

            if stype == "reply":
                if not include_reply:
                    continue
                rid = str(data.get("id") or "").strip()
                if not rid:
                    continue
                # Only a quote the target can actually resolve is worth
                # sending: every implementation resolves the reply from the
                # same store get_msg reads, so an unresolvable quote would
                # render as a broken/empty bubble.
                if not await self._reply_resolvable(client, rid):
                    note("reply", "quoted message unresolvable")
                    continue
                if self.reply_mode == "drop":
                    note("reply", "reply_mode=drop")
                    continue
                if self.reply_mode == "textify":
                    text = await self._reply_text(client, rid)
                    if text:
                        usable.append({"type": "text", "data": {"text": text}})
                        continue
                if impl == _IMPL_SNOWLUMA:
                    # SnowLuma's forward parser has no resolveReplySequence, so
                    # a positive reply id is consumed as a QQ sequence.
                    seq = await self._reply_seq(client, rid)
                    if seq <= 0:
                        note("reply", "quoted message has no QQ sequence")
                        continue
                    usable.append({"type": "reply", "data": {"id": str(seq)}})
                    continue
                # NapCat / LLOneBot: keep the ORIGINAL message id. Never a
                # rewritten seq — NapCat overwrites message_seq with its short
                # id and LLOneBot only resolves replies through its short id.
                usable.append({"type": "reply", "data": {"id": rid}})
                continue

            if stype == "forward":
                # All three implementations allow at most 3 forward-card levels
                # (NapCat dp>=3 break, SnowLuma MAX_FORWARD_DEPTH=3). The outer
                # card is level 1, so a nested forward may only be expanded
                # while depth + 1 < max_depth.
                if not include_nested or depth + 1 >= self.max_depth:
                    note("forward", "nesting depth cap")
                    depth_capped = True
                    continue
                res_id = str(data.get("id") or "").strip()
                inner = await self._fetch_forward(client, res_id) if res_id else None
                if not inner:
                    note("forward", "get_forward_msg failed")
                    continue
                inner_nodes = []
                for im in inner:
                    node = await self._build_content_node(
                        client, im, is_group, impl, depth + 1,
                        include_reply=include_reply, include_nested=include_nested,
                        loss=loss, mid=str(im.get("message_id") or ""),
                    )
                    if node:
                        inner_nodes.append(node)
                if inner_nodes:
                    nested = inner_nodes
                else:
                    note("forward", "inner forward empty")
                continue

            # music / poke / shake / unknown: not reconstructable in a content
            # node on any of the three implementations.
            note(stype or "?", "unsupported in a content node")

        # SnowLuma rejects a video that has sibling segments.
        if impl == _IMPL_SNOWLUMA and has_video and len(usable) > 1:
            usable = [s for s in usable if s.get("type") == "video"][:1]

        # A c2c forward node can carry at most one file element on SnowLuma
        # (assertPrivateForwardFileCapacity).
        if impl == _IMPL_SNOWLUMA and not is_group:
            seen_file = False
            filtered = []
            for seg in usable:
                if seg.get("type") == "file":
                    if seen_file:
                        note("file", "c2c allows one file per node")
                        continue
                    seen_file = True
                filtered.append(seg)
            usable = filtered

        if not usable and nested is None and depth_capped:
            # The only content was a forward nested deeper than the 3-level
            # limit every implementation enforces; keep the node visible.
            usable = [{"type": "text", "data": {"text": "[聊天记录]"}}]

        if nested is not None:
            if usable:
                # A nested forward card must be the node's entire content
                # (SnowLuma/LLOneBot/NapCat all require a pure node array).
                logger.info(
                    "[forward_fix] nested forward replaces %d sibling segment(s)",
                    len(usable),
                )
            content = nested
        elif usable:
            content = usable
        else:
            return None

        if dropped:
            logger.info("[forward_fix] content node %s dropped segments: %s",
                        uid, dropped)

        data = {
            "user_id": str(uid),
            "nickname": str(nick or uid),
            "time": self._msg_time(msg),
            "content": content,
        }
        return {"type": "node", "data": data}

    @staticmethod
    def _segments(msg: dict) -> list | None:
        segs = msg.get("message")
        if isinstance(segs, list):
            return segs
        # message_format="string" connections expose a CQ string; content
        # reconstruction would be lossy, so let the caller fall back.
        return None

    @staticmethod
    def _sender_info(msg: dict) -> tuple[str, str]:
        sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
        uid = msg.get("user_id") or sender.get("user_id") or ""
        nick = (
            msg.get("card") or msg.get("nickname")
            or sender.get("card") or sender.get("nickname") or ""
        )
        return str(uid) if uid else "", str(nick) if nick else ""

    @staticmethod
    def _msg_time(msg: dict) -> int:
        try:
            t = int(msg.get("time") or 0)
        except (TypeError, ValueError):
            t = 0
        return t if t > 0 else int(time.time())

    @staticmethod
    def _segment_safe(stype: str, data: dict) -> bool:
        """False when the rebuilt segment would fail a target's hard validation
        (SnowLuma throws for json without data; mface needs emoji_id)."""
        if stype == "json":
            value = data.get("data")
            if value is None:
                return False
            if isinstance(value, str) and not value.strip():
                return False
        if stype == "mface" and not str(data.get("emoji_id") or "").strip():
            return False
        return True

    @staticmethod
    def _scalar_data(stype: str, data: dict) -> dict:
        out = {}
        for field in _SEGMENT_FIELDS.get(stype, ()):
            value = data.get(field)
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                out[field] = value
            elif stype == "json" and field in ("data", "config") and isinstance(value, (dict, list)):
                out[field] = value
        return out

    @staticmethod
    def _is_loadable(source: str) -> bool:
        if not source:
            return False
        if source.startswith(("http://", "https://", "file://", "base64://", "data:")):
            return True
        return "/" in source or "\\" in source

    def _media_data(self, stype: str, data: dict) -> dict | None:
        url = str(data.get("url") or "").strip()
        raw_file = str(data.get("file") or "").strip()
        path = str(data.get("path") or "").strip()
        file_id = str(data.get("file_id") or "").strip()

        best = ""
        if self._is_loadable(url):
            best = url
        elif self._is_loadable(raw_file):
            best = raw_file
        elif path and os.path.exists(path):
            best = "file://" + path
        if not best:
            return None

        out = {"file": best}
        if self._is_loadable(url):
            out["url"] = url
        if file_id:
            out["file_id"] = file_id
        if path:
            out["path"] = path
        for field in ("name", "file_size", "summary", "sub_type", "subType", "type",
                      "thumb", "cover", "emoji_id", "emoji_package_id", "key"):
            value = data.get(field)
            if isinstance(value, (str, int, float, bool)) and value not in ("", None):
                out[field] = value
        return out

    def _file_data(self, data: dict, impl: str) -> dict | None:
        # NapCat's content-node file handler downloads data.url; an expired
        # history url fails the whole forward, so files never enter content
        # nodes for NapCat (id nodes forward the real file natively).
        if impl == _IMPL_NAPCAT:
            return None

        url = str(data.get("url") or "").strip()
        path = str(data.get("path") or "").strip()
        file_id = str(data.get("file_id") or data.get("id") or "").strip()
        name = str(data.get("name") or data.get("file") or "").strip()

        out: dict = {}
        if impl == _IMPL_SNOWLUMA:
            # SnowLuma's prepareForwardFileElement only re-uploads from `url`
            # when there is NO file_id; with both set it takes the fileId branch
            # and may throw when the file is not cached for the target scope.
            if self._is_loadable(url):
                out["url"] = url
            elif path and os.path.exists(path):
                out["file"] = "file://" + path
            elif file_id:
                out["file_id"] = file_id
        else:
            if self._is_loadable(url):
                out["url"] = url
            elif path and os.path.exists(path):
                out["file"] = "file://" + path
        if not out:
            return None
        if name:
            out["name"] = name
        for field in ("file_size",):
            value = data.get(field)
            if isinstance(value, (str, int, float, bool)) and value not in ("", None):
                out[field] = value
        return out

    # --------------------------------------------------------------- previews

    def _preview_of(self, msg: dict) -> str:
        segs = self._segments(msg) or []
        parts = []
        for seg in segs:
            stype = seg.get("type")
            if stype == "text":
                text = str((seg.get("data") or {}).get("text") or "").strip()
                if text:
                    parts.append(text[:40])
            elif stype == "at":
                parts.append("@" + str((seg.get("data") or {}).get("qq") or ""))
            else:
                preview = _PREVIEW.get(stype)
                if preview:
                    parts.append(preview)
        return " ".join(parts).strip() or "[消息]"

    def _card_meta(self, resolved, is_group: bool) -> dict:
        news = []
        nicks = []
        for _mid, msg in resolved:
            _uid, nick = self._sender_info(msg)
            label = nick or "QQ用户"
            if len(news) < 4:
                news.append({"text": f"{label}: {self._preview_of(msg)}"})
            if label not in nicks and len(nicks) < 4:
                nicks.append(label)
        if is_group:
            source = "群聊的聊天记录"
        else:
            source = "和".join(nicks) + "的聊天记录" if nicks else "聊天记录"
        return {
            "source": source,
            "summary": f"查看{len(resolved)}条转发消息",
            "prompt": "[聊天记录]",
            "news": news,
        }

    # ------------------------------------------------------------------ send

    async def _send_nodes(self, client, is_group: bool, session_id: str,
                          nodes: list[dict], resolved) -> bool:
        action = "send_group_forward_msg" if is_group else "send_private_forward_msg"
        try:
            payload = {
                "messages": nodes,
                "message": nodes,
            }
            payload.update(self._card_meta(resolved, is_group))
            if is_group:
                payload["group_id"] = int(session_id)
                action = "send_group_forward_msg"
            else:
                payload["user_id"] = int(session_id)
                action = "send_private_forward_msg"

            if self.debug_dump:
                logger.info("[forward_fix] payload %s: %s", action, payload)
            result = await client.send_action(action, payload, timeout=60)
            if isinstance(result, dict):
                if result.get("status") == "ok" or str(result.get("retcode")) == "0":
                    return True
                logger.error("[forward_fix] %s rejected: %s", action, result)
                return False
            return result is None or bool(result)
        except Exception as e:
            logger.error("[forward_fix] %s raised: %s", action, e, exc_info=self.debug)
            return False

    async def _forward_single(self, client, is_group: bool, session_id: str, mid) -> bool:
        try:
            if is_group:
                action = "forward_group_single_msg"
                payload = {"message_id": str(mid), "group_id": str(session_id)}
            else:
                action = "forward_friend_single_msg"
                payload = {"message_id": str(mid), "user_id": str(session_id)}
            result = await client.send_action(action, payload, timeout=30)
            if isinstance(result, dict):
                return result.get("status") == "ok" or str(result.get("retcode")) == "0"
            return result is None or bool(result)
        except Exception as e:
            logger.debug("[forward_fix] %s failed: %s", "single forward", e)
            return False

    async def _report_loss(self, sid: str, loss: list):
        if not loss:
            return
        logger.warning("[forward_fix] forwarded with %d dropped item(s): %s",
                       len(loss), loss)
        if not self.loss_report:
            return
        lines = []
        for item in loss[:8]:
            lines.append("- %s %s: %s" % (item.get("type"), item.get("mid"), item.get("reason")))
        if len(loss) > 8:
            lines.append("- ... 另有 %d 项" % (len(loss) - 8))
        try:
            await self.ctx.send_message_chain(
                sid, MessageChain([Text("合并转发完成，但以下内容未能转发：\n" + "\n".join(lines))])
            )
        except Exception as e:
            logger.error("[forward_fix] loss report error: %s", e)

    async def _notify_failure(self, sid: str):
        try:
            await self.ctx.send_message_chain(
                sid, MessageChain([Text("合并转发发送失败，请稍后重试")])
            )
        except Exception as e:
            logger.error("[forward_fix] failure notice error: %s", e)
