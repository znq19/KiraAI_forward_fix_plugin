import logging
from core.plugin import BasePlugin, on, Priority
from core.chat import MessageChain
from core.chat.message_elements import Forward, Text
from core.tag import RootTagAction

logger = logging.getLogger(__name__)

# KiraAI sid format: <adapter>:<dm|gm>:<id> (see core/message_manager.py
# send_xml_messages / send_message_chain). Only these two session types exist.
_GROUP_SESSION_TYPES = {"gm"}
_PRIVATE_SESSION_TYPES = {"dm"}

# Segment types whose structure cannot be rebuilt from content (file upload,
# nested forward, reply reference, card, music signature). These must keep
# the original message ID node so NapCat preserves the structure.
_ID_NODE_TYPES = {"file", "forward", "reply", "json", "music"}


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
    - Sending nodes by {"id": ...} requires NapCat to look up each message's
      sender; lookup fails with "has no valid sender user_id" when the ID is
      not in NapCat's cache (stale / cross-session / hallucinated by the LLM).
    - Fix: fetch the real message history via get_group_msg_history /
      get_friend_msg_history, match IDs, and build content nodes
      (user_id + time + content) for ordinary messages so no ID lookup is
      needed. Special segments (file/forward/reply/card/music) keep ID nodes.
      If fewer than half the IDs match history, the LLM likely hallucinated
      them - fall back to the latest N history messages.
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

            # 1. Fetch real history (read from NapCat's local DB, reliable).
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

            nodes = []
            matched = 0
            for mid in message_ids:
                msg = by_id.get(str(mid))
                if msg is not None:
                    matched += 1
                    if self._needs_id_node(msg):
                        # Structure-preserving segments keep the ID node.
                        nodes.append({"type": "node", "data": {"id": str(mid)}})
                    else:
                        node = self._build_content_node(msg)
                        if node:
                            nodes.append(node)
                            continue
                        nodes.append({"type": "node", "data": {"id": str(mid)}})
                else:
                    # Unmatched ID: try the ID node as a last resort.
                    nodes.append({"type": "node", "data": {"id": str(mid)}})

            # 3. Low match rate -> the LLM hallucinated the IDs (common when
            #    it was asked to "forward the latest N messages"). Fall back
            #    to the latest N history messages as content nodes.
            if matched < len(message_ids) * 0.5:
                logger.warning(
                    "[forward_fix] only %d/%d message IDs matched history; "
                    "falling back to latest %d messages",
                    matched, len(message_ids), len(message_ids),
                )
                nodes = []
                for m in history[: len(message_ids)]:
                    node = self._build_content_node(m)
                    if node:
                        nodes.append(node)
                if not nodes:
                    logger.error("[forward_fix] no usable history messages")
                    return False

            if not nodes:
                logger.error("[forward_fix] no nodes to send")
                return False

            # 4. Send via the dedicated OneBot API.
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
                # Sort newest first by timestamp (some implementations return
                # oldest first).
                return sorted(
                    messages, key=lambda m: m.get("time") or 0, reverse=True
                )
        except Exception as e:
            logger.error("[forward_fix] history fetch failed: %s", e)
        return []

    @staticmethod
    def _needs_id_node(msg: dict) -> bool:
        """True if the message contains segments that need the original ID."""
        for seg in msg.get("message") or []:
            if seg.get("type") in _ID_NODE_TYPES:
                return True
        return False

    @staticmethod
    def _build_content_node(msg: dict) -> dict | None:
        """Build a content node (user_id + time + content) from a history msg."""
        segs = msg.get("message") or []
        user_id = msg.get("user_id")
        if not segs or not user_id:
            return None
        return {
            "type": "node",
            "data": {
                # NapCat requires string user_id for forward nodes.
                "user_id": str(user_id),
                "time": int(msg.get("time") or 0),
                "content": segs,
            },
        }

    async def _notify_failure(self, sid: str):
        """Optional user-visible failure notice (off by default)."""
        try:
            await self.ctx.send_message_chain(
                sid, MessageChain([Text("合并转发发送失败，请稍后重试")])
            )
        except Exception as e:
            logger.error("[forward_fix] failure notice error: %s", e)
