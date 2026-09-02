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
    - Fix: intercept at after_xml_parse, drop the Forward element, and call
      the dedicated OneBot API send_group_forward_msg / send_private_forward_msg
      directly, bypassing the built-in sender.

    Design:
    - Invisible to the LLM: no new tools, no prompt changes.
    - Non-destructive: only touches the actions list for Forward elements.
    - No impact on other plugins: high priority, only handles Forward types.
    - Silent by default: no feedback text on success or failure (configurable).
    - Group / private chat: decided by the sid session type (gm / dm).
    - Real media forwarding: references original messages by ID, QQ client
      pulls the full original content automatically.
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

            # Standard OneBot v11 node list; negative IDs are local message
            # IDs and are valid here (the built-in sender failed because it
            # sent node segments outside a forward node list, not because of
            # the IDs themselves).
            nodes = [{"type": "node", "data": {"id": str(mid)}} for mid in message_ids]

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

    async def _notify_failure(self, sid: str):
        """Optional user-visible failure notice (off by default)."""
        try:
            await self.ctx.send_message_chain(
                sid, MessageChain([Text("合并转发发送失败，请稍后重试")])
            )
        except Exception as e:
            logger.error("[forward_fix] failure notice error: %s", e)
