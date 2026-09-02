import logging
import time
from core.plugin import BasePlugin, on, Priority
from core.chat import MessageChain
from core.chat.message_elements import Forward, Text
from core.tag import RootTagAction

logger = logging.getLogger(__name__)

# KiraAI sid format: <adapter>:<dm|gm>:<id> (see core/message_manager.py
# send_xml_messages / send_message_chain). Only these two session types exist.
_GROUP_SESSION_TYPES = {"gm"}
_PRIVATE_SESSION_TYPES = {"dm"}

# Segment types that MUST keep the original message ID node because their
# content cannot be rebuilt (file upload, music signature). Nested forward
# is expanded via get_forward_msg into content nodes (multi-level forward);
# reply segments are textified to [引用 msg_id:xxx] so rendering does not
# depend on the implementation's reply lookup.
_ID_NODE_TYPES = {"file", "music"}


class ForwardFixPlugin(BasePlugin):
    """
    Transparently fix KiraAI's built-in <forward> merge-forward feature.

    Root cause (verified against KiraAI v2.33.1 source):
    - Built-in ForwardTag (core/plugin/builtin_plugins/kira-ai/tags.py) parses
      <forward merge="true">id1,id2</forward> into a Forward element inside a
      MessageChain.
    - Built-in QQAdapter._send_forward (core/adapter/src/qq/qq.py) sends the
      merge case via send_group_segments -> send_action("send_group_msg", ...)
      with {"type": "node"} segments. OneBot rejects node segments outside a
      forward node list with retcode 1400.

    Second failure mode (fixed in v1.2.0):
    - Sending nodes by {"id": ...} requires the OneBot implementation to look
      up each message's sender. NapCat resolves via the NTQQ client DB, but
      SnowLuma / LLOneBot resolve via their own in-memory / SQLite message
      store, which only contains messages seen since startup. IDs from the
      HTTP history API (a different ID namespace) or from before a restart
      are not found -> "has no valid sender user_id".

    v1.5.5 strategy (ID-node-first with content-node fallback):
    - Resolve the LLM's IDs against real history (get_group_msg_history /
      get_friend_msg_history) and get_msg, exactly like before.
    - Send ID nodes FIRST ({type: node, data: {id}}). This matches the
      built-in behavior: KiraAI without this plugin sends node segments
      and NapCat resolves each ID from the NTQQ client DB, so images,
      files and nested forwards all render natively. On NapCat / LLOneBot
      the first send succeeds.
    - If the ID-node send fails (SnowLuma / LLOneBot cannot look up the
      sender), rebuild EVERYTHING as content nodes (user_id + nickname +
      time + content; file/music segments dropped, reply segments keep
      their ORIGINAL message_id - every implementation resolves the real
      quote from it, nested forwards expanded via get_forward_msg) and
      retry once.
    - If fewer than half the IDs match history, the LLM likely hallucinated
      them - fall back to the latest N history messages (real IDs).
    """

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        sec = cfg.get("section_main", {})
        self.silent_fail = sec.get("silent_fail", True)

    async def initialize(self):
        logger.info("[forward_fix] initialized")

    async def terminate(self):
        pass

    @on.after_xml_parse(priority=Priority.HIGH)
    async def fix_forward(self, event, actions: list, *_):
        # Platform gate: QQ / OneBot only. event.adapter is an AdapterInfo
        # whose platform comes from the user's adapter config ("QQ" or "qq").
        if getattr(event, "adapter", None) is None:
            return
        platform = getattr(event.adapter, "platform", "") or ""
        if platform.lower() != "qq":
            return

        # after_xml_parse receives a KiraMessageBatchEvent whose sid property
        # resolves to session.sid; keep the session fallback for safety.
        sid = getattr(event, "sid", None) or getattr(
            getattr(event, "session", None), "sid", None
        )
        if not sid:
            return
        parts = sid.split(":", 2)
        if len(parts) != 3:
            return
        adapter_name, session_type, session_id = parts

        # Only handle known group / private session types; never guess.
        is_group = session_type in _GROUP_SESSION_TYPES
        if not is_group and session_type not in _PRIVATE_SESSION_TYPES:
            return

        i = 0
        while i < len(actions):
            action = actions[i]
            message_ids = None
            remove_action = False  # whether the whole action should be dropped

            # Case A: root tag <forward>...</forward> (defensive; the built-in
            # ForwardTag has parent="msg", so this normally never occurs).
            if isinstance(action, RootTagAction):
                tag_name = getattr(action.tag, "name", None)
                if tag_name == "forward":
                    message_ids = self._parse_ids(action.value)
                    remove_action = True

            # Case B: MessageChain containing Forward element(s) - the real
            # path for the built-in <forward> tag.
            elif isinstance(action, MessageChain):
                has_forward = False
                new_list = []
                for elem in action.message_list:
                    if isinstance(elem, Forward):
                        has_forward = True
                        ids = self._parse_ids(elem.message_id)
                        if ids:
                            message_ids = (message_ids or []) + ids
                    else:
                        new_list.append(elem)

                if has_forward:
                    # Only strip the Forward element(s) when at least one ID
                    # was parsed. Otherwise keep the chain untouched so the
                    # built-in sender reports the failure visibly instead of
                    # silently dropping the message.
                    if message_ids:
                        action.message_list = new_list
                        if not new_list:
                            remove_action = True
                    else:
                        logger.warning(
                            "[forward_fix] Forward element with unparsable IDs "
                            "left to built-in sender: %r",
                            [getattr(e, "message_id", None) for e in action.message_list],
                        )

            if message_ids:
                ok = await self._send_forward(
                    adapter_name, is_group, session_id, message_ids
                )
                if ok:
                    if remove_action:
                        actions.pop(i)
                        i -= 1
                    logger.info(
                        "[forward_fix] forwarded %d messages in %s:%s",
                        len(message_ids), session_type, session_id,
                    )
                else:
                    # Drop the action either way: keeping it would make the
                    # built-in sender fail again and double-log the error.
                    if remove_action:
                        actions.pop(i)
                        i -= 1
                    logger.error(
                        "[forward_fix] forward FAILED (%d messages, %s:%s) - "
                        "kept silent",
                        len(message_ids), session_type, session_id,
                    )
                    if not self.silent_fail:
                        await self._notify_failure(sid)

            i += 1

    def _parse_ids(self, raw) -> list[int] | None:
        """Parse message IDs from Forward.message_id / RootTagAction.value."""
        if not raw:
            return None

        id_strs = []
        if isinstance(raw, str):
            id_strs = [x.strip() for x in raw.split(",") if x.strip()]
        elif isinstance(raw, list):
            id_strs = [str(x).strip() for x in raw if str(x).strip()]

        message_ids = []
        seen = set()
        for s in id_strs:
            try:
                mid = int(s)
                if mid not in seen:
                    seen.add(mid)
                    message_ids.append(mid)
            except ValueError:
                continue
        return message_ids if message_ids else None

    async def _send_forward(
        self, adapter_name: str, is_group: bool, session_id: str, message_ids: list[int]
    ) -> bool:
        """Send merge-forward via the OneBot v11 dedicated API."""
        try:
            adapter = self.ctx.adapter_mgr.get_adapter(adapter_name)
            if adapter is None:
                logger.error("[forward_fix] adapter %r not found", adapter_name)
                return False
            client = adapter.get_client()
            if client is None:
                logger.error("[forward_fix] adapter %r has no client", adapter_name)
                return False

            # 1. Fetch real history (read from the OneBot implementation's
            #    local store, reliable).
            fetch_count = max(len(message_ids) * 2, 20)
            history = await self._fetch_history(
                client, is_group, session_id, fetch_count
            )

            # 2. Match the LLM-provided IDs against history.
            by_id = {}
            for m in history:
                mid = m.get("message_id")
                if mid is not None:
                    by_id[str(mid)] = m

            resolved = []  # (mid, msg) pairs, in the LLM's order
            matched = 0
            missing = []
            for mid in message_ids:
                msg = by_id.get(str(mid))
                if msg is not None:
                    resolved.append((str(mid), msg))
                    matched += 1
                else:
                    missing.append(mid)

            # 3. Resolve IDs missing from history one-by-one via get_msg.
            #    This is the "point at what you mean" path: the LLM may have
            #    referenced a message outside the recent window (e.g. a reply
            #    target). get_msg works on NapCat (client DB) and SnowLuma
            #    (message store); only truly unknown IDs are skipped.
            for mid in missing:
                msg = await self._fetch_msg(client, mid)
                if msg is not None:
                    resolved.append((str(mid), msg))
                    matched += 1
                else:
                    logger.warning(
                        "[forward_fix] message_id %s not found via get_msg; skipped",
                        mid,
                    )

            # 4. If most IDs could not be resolved, the LLM likely
            #    hallucinated them. Fall back to the latest N history
            #    messages (real IDs, resolvable by every implementation).
            if matched < len(message_ids) * 0.5:
                logger.warning(
                    "[forward_fix] only %d/%d message IDs resolved; "
                    "falling back to latest %d messages",
                    matched, len(message_ids), len(message_ids),
                )
                resolved = []
                for m in history[: len(message_ids)]:
                    mid = m.get("message_id")
                    if mid is not None:
                        resolved.append((str(mid), m))
                if not resolved:
                    logger.error("[forward_fix] no usable history messages")
                    return False

            # 5. ID nodes FIRST - this matches the built-in behavior
            #    (KiraAI without this plugin sends node segments and NapCat
            #    resolves each ID from the NTQQ client DB, so images, files
            #    and nested forwards all render natively). On NapCat /
            #    LLOneBot this succeeds on the first try; on SnowLuma the
            #    ID lookup fails and we fall back to content nodes below.
            id_nodes = [
                {"type": "node", "data": {"id": int(mid)}}
                for mid, _ in resolved
            ]
            result = await self._send_nodes(client, is_group, session_id, id_nodes)
            if result is True:
                return True

            # 6. Fallback: rebuild everything as content nodes (file/music
            #    segments dropped, reply segments rewritten to the
            #    authoritative QQ seq, nested forwards expanded) and retry
            #    once. This is the SnowLuma / LLOneBot path.
            logger.warning(
                "[forward_fix] ID-node send failed; retrying with %d "
                "content nodes",
                len(resolved),
            )
            content_nodes = []
            for mid, msg in resolved:
                node = await self._build_node(
                    client, msg, mid, force_content=True
                )
                if node:
                    content_nodes.append(node)
            if not content_nodes:
                logger.error("[forward_fix] no content nodes to send")
                return False
            result = await self._send_nodes(
                client, is_group, session_id, content_nodes
            )
            if result is True:
                return True

            # 7. Fuse: if the send still failed and any content node carries
            #    a native reply segment (probe said renderable but the
            #    implementation rejected it), drop those nodes entirely and
            #    retry once - every forwarded message stays real (native
            #    quotes only), and the rest still goes through.
            if any(self._node_has_reply(n) for n in content_nodes):
                no_reply = [
                    n for n in content_nodes if not self._node_has_reply(n)
                ]
                if no_reply:
                    logger.warning(
                        "[forward_fix] reply-segment send failed; retrying "
                        "without %d reply-carrying nodes",
                        len(content_nodes) - len(no_reply),
                    )
                    result = await self._send_nodes(
                        client, is_group, session_id, no_reply
                    )
                    if result is True:
                        return True
            return False

        except Exception as e:
            logger.error("[forward_fix] OneBot API raised: %s", e)
            return False

    @staticmethod
    def _node_has_reply(node: dict) -> bool:
        """True when a content node contains a native reply segment."""
        for seg in (node.get("data") or {}).get("content") or []:
            if seg.get("type") == "reply":
                return True
        return False

    async def _send_nodes(
        self, client, is_group: bool, session_id: str, nodes: list[dict]
    ) -> bool:
        """Send a node list via the dedicated OneBot API."""
        try:
            if is_group:
                result = await client.send_action(
                    "send_group_forward_msg",
                    {"group_id": int(session_id), "messages": nodes},
                    timeout=30,
                )
            else:
                result = await client.send_action(
                    "send_private_forward_msg",
                    {"user_id": int(session_id), "messages": nodes},
                    timeout=30,
                )

            # OneBot v11 returns a dict on failure WITHOUT raising
            # (NapCatWebSocketClient.send_action resolves the future with the
            # raw response). Timeouts / login failures raise instead.
            if isinstance(result, dict):
                status = result.get("status")
                retcode = result.get("retcode")
                if status == "ok" or str(retcode) == "0":
                    return True
                logger.error("[forward_fix] OneBot API error: %s", result)
                return False
            # Some implementations return None / truthy on success.
            return result is None or bool(result)
        except Exception as e:
            logger.error("[forward_fix] OneBot API raised: %s", e)
            return False

    async def _fetch_history(
        self, client, is_group: bool, session_id: str, count: int
    ) -> list[dict]:
        """Fetch recent message history via OneBot API (newest first)."""
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
                messages = (resp.get("data") or {}).get("messages") or []
                # SnowLuma returns newest-first already; only re-sort when
                # timestamps are present and meaningful (some implementations
                # return time=0 for stored messages, which would scramble the
                # order).
                if messages and any(m.get("time") for m in messages):
                    messages = sorted(
                        messages, key=lambda m: m.get("time") or 0, reverse=True
                    )
                return messages
        except Exception as e:
            logger.error("[forward_fix] history fetch failed: %s", e)
        return []

    async def _fetch_msg(self, client, message_id: int) -> dict | None:
        """Fetch a single message by ID via get_msg (OneBot v11)."""
        try:
            resp = await client.send_action(
                "get_msg", {"message_id": message_id}, timeout=15
            )
            if isinstance(resp, dict) and resp.get("status") == "ok":
                data = resp.get("data") or {}
                if data.get("message"):
                    return data
        except Exception as e:
            logger.error("[forward_fix] get_msg(%s) failed: %s", message_id, e)
        return None

    async def _build_node(
        self, client, msg: dict, mid: str, force_content: bool = False
    ) -> dict | None:
        """Build a node for a message: content node, or ID node for
        structure-sensitive segments (file / music).
        With force_content=True (the fallback path after an ID-node send
        failed), file / music messages are rebuilt as content nodes with
        those segments dropped - an ID node would fail again for the same
        reason the first send failed.
        Nested forward segments are EXPANDED via get_forward_msg into
        nested content nodes - SnowLuma's uploadRecursive and NapCat's
        uploadForwardedNodesPacket both recognize nested node arrays and
        render real multi-level forward cards. (Keeping {type: forward,
        data: {id: message_id}} would point at a non-existent res_id and
        render an empty card - the "点进去是空的" bug.)
        Reply segments keep the ORIGINAL message_id - NO seq rewrite.
        (v1.5.2 rewrote {id: seq, seq: seq} from get_msg's message_seq,
        but NapCat's get_msg OVERWRITES message_seq with the short-hash
        message_id - it is NOT the QQ authoritative sequence. Sending
        that fake seq made NapCat's seq-first lookup fetch the WRONG
        message (引用错位), and SnowLuma's resolveReplySequence(messageId)
        failed the store lookup and fell back to a broken seq (旧版抛
        "missing required or usable fields").)
        Every implementation resolves the reply from the ORIGINAL id:
          - NapCat: id branch -> getMsgIdAndPeerByShortId -> real message
          - SnowLuma: resolveReplySequence(id) -> store lookup -> real seq
          - LLOneBot: standard OneBot reply, id compatible.
        Probe: get_msg must resolve the quoted message (same store the
        implementation uses for reply lookup - get_msg works ⇔ the reply
        renders). Unresolvable replies have their segment DROPPED while
        the rest of the message is kept - never textified, never sent
        with a broken reply, never dropped entirely.
        Returns None when the message cannot be represented (missing
        user_id / empty content) - the caller skips it instead of
        falling back to an ID node, because an unresolvable ID node fails
        the whole forward on SnowLuma / LLOneBot."""
        if not force_content and self._needs_id_node(msg):
            return {"type": "node", "data": {"id": mid}}
        # Probe reply targets: the set of reply ids that get_msg can
        # resolve (and therefore every implementation can render).
        resolvable = set()
        for seg in msg.get("message") or []:
            if seg.get("type") == "reply":
                rid = (seg.get("data") or {}).get("id", "")
                if not rid:
                    continue
                try:
                    quoted = await self._fetch_msg(client, int(rid))
                    if quoted is not None and quoted.get("message"):
                        resolvable.add(str(rid))
                except (TypeError, ValueError):
                    continue
        return await self._build_content_node(client, msg, resolvable)

    @staticmethod
    def _needs_id_node(msg: dict) -> bool:
        """True if the message contains segments that need the original ID."""
        for seg in msg.get("message") or []:
            if seg.get("type") in _ID_NODE_TYPES:
                return True
        return False

    async def _build_content_node(
        self, client, msg: dict, resolvable_replies: set | None = None, depth: int = 0
    ) -> dict | None:
        """Build a content node (user_id + nickname + time + content) from a
        history / get_msg message. Reply segments keep their ORIGINAL
        message_id (in `resolvable_replies`, the set of reply ids that
        get_msg resolved) - every implementation resolves the quote from
        that id (NapCat: short-id lookup; SnowLuma: store lookup; LLOneBot:
        standard). Unresolvable reply segments are DROPPED while the rest
        of the message is kept. Nested forward segments are expanded via
        get_forward_msg into nested content nodes (real multi-level cards).
        time is ALWAYS provided (msg.time or now) - omitting it renders
        1970 on older SnowLuma builds."""
        if depth > 3:
            return None
        segs = msg.get("message") or []
        # Some implementations (e.g. SnowLuma) nest the sender under
        # `sender.user_id` instead of a top-level `user_id`.
        user_id = msg.get("user_id") or (msg.get("sender") or {}).get("user_id")
        if not segs or not user_id:
            return None
        # Media segments without a usable source (file/url) would fail the
        # whole forward on SnowLuma ("requires a file/url source"). Drop
        # those segments; if nothing remains, the node cannot be built.
        usable = []
        nested_nodes = None
        for seg in segs:
            stype = seg.get("type")
            if stype in ("image", "record", "video"):
                data = seg.get("data") or {}
                if not (data.get("file") or data.get("url") or data.get("file_id")):
                    continue
            elif stype in ("file", "music"):
                # File / music segments are ALWAYS dropped from content
                # nodes. NapCat's handleOb11FileLikeMessage tries to
                # download data.url - history URLs are usually expired, the
                # download fails with "element not found" and fails the
                # WHOLE forward. Outer file messages keep the ID node
                # instead (NapCat resolves the real file from the client
                # DB - true file forwarding without URL downloads).
                continue
            elif stype == "reply":
                rid = (seg.get("data") or {}).get("id", "")
                if rid and str(rid) in (resolvable_replies or set()):
                    # Keep the ORIGINAL message_id - every implementation
                    # resolves the real quote from it (NapCat short-id
                    # lookup, SnowLuma store lookup, LLOneBot standard).
                    usable.append({"type": "reply", "data": {"id": str(rid)}})
                else:
                    # Unresolvable reply: drop the segment, keep the rest
                    # of the message (never textify, never drop the whole
                    # message, never send a broken reply).
                    continue
                continue
            elif stype == "forward":
                # Expand the nested forward via get_forward_msg: the stored
                # data.id is the res_id (receive-side codec), so pass it
                # through. The expanded nodes become the ENTIRE content as a
                # pure node array - SnowLuma's isNestedNodeArray requires
                # content to be all-node, and QQ never mixes a forward card
                # with other segments in one message.
                fid = (seg.get("data") or {}).get("id", "")
                if fid:
                    inner = await self._fetch_forward(client, fid)
                    if inner:
                        nested = []
                        for im in inner:
                            inode = await self._build_content_node(
                                client, im, None, depth + 1
                            )
                            if inode:
                                nested.append(inode)
                        if nested:
                            nested_nodes = nested
                continue
            usable.append(seg)
        if not usable and not nested_nodes:
            return None
        # Prefer the group card (card), then the nickname, so QQ shows the
        # real name instead of falling back to the QQ number.
        sender = msg.get("sender") or {}
        nickname = (
            msg.get("card")
            or msg.get("nickname")
            or sender.get("card")
            or sender.get("nickname")
            or ""
        )
        data = {
            # NapCat requires string user_id for forward nodes.
            "user_id": str(user_id),
            "content": nested_nodes if nested_nodes is not None else usable,
        }
        # ALWAYS provide time: omitting it renders 1970 on older SnowLuma
        # builds (no now-fallback in buildForwardPushBody).
        msg_time = int(msg.get("time") or 0)
        data["time"] = msg_time if msg_time > 0 else int(time.time())
        if nickname:
            data["nickname"] = str(nickname)
        return {"type": "node", "data": data}

    async def _fetch_forward(self, client, res_id: str) -> list | None:
        """Fetch a nested forward's messages via get_forward_msg (OneBot v11
        extended action; SnowLuma supports it, NapCat/LLOneBot too)."""
        try:
            resp = await client.send_action(
                "get_forward_msg", {"id": res_id}, timeout=15
            )
            if isinstance(resp, dict) and resp.get("status") == "ok":
                return resp.get("data", {}).get("messages") or []
        except Exception as e:
            logger.error("[forward_fix] get_forward_msg(%s) failed: %s", res_id, e)
        return None

    async def _notify_failure(self, sid: str):
        """Optional user-visible failure notice (off by default)."""
        try:
            await self.ctx.send_message_chain(
                sid, MessageChain([Text("合并转发发送失败，请稍后重试")])
            )
        except Exception as e:
            logger.error("[forward_fix] failure notice error: %s", e)
