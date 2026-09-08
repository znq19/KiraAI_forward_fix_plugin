# -*- coding: utf-8 -*-
"""Minimal KiraAI framework mock so the real forward_fix main.py can be
imported and its after_xml_parse hook invoked directly."""
import asyncio
import logging
import sys
import types


def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


logger = logging.getLogger("mock")
logger.addHandler(logging.NullHandler())

# ── core.plugin ──
class BasePlugin:
    def __init__(self, ctx, cfg):
        self.ctx = ctx
        self.cfg = cfg


class Priority:
    SYS_LOW = -100
    LOW = -50
    MEDIUM = 0
    HIGH = 50
    SYS_HIGH = 100


def _passthrough_decorator(*d_args, **d_kwargs):
    def deco(fn):
        fn._registered = (d_args, d_kwargs)
        return fn
    return deco


class _Register:
    tool = staticmethod(_passthrough_decorator)
    tag = staticmethod(_passthrough_decorator)


class _On:
    after_xml_parse = staticmethod(_passthrough_decorator)
    llm_request = staticmethod(_passthrough_decorator)
    im_message = staticmethod(_passthrough_decorator)


core = _mod("core")
cp = _mod("core.plugin")
cp.BasePlugin = BasePlugin
cp.logger = logger
cp.on = _On()
cp.Priority = Priority
cp.register = _Register()

# ── core.chat ──
cc = _mod("core.chat")


class MessageChain:
    def __init__(self, elements=None):
        self.message_list = list(elements or [])

    def __iter__(self):
        return iter(self.message_list)


cc.MessageChain = MessageChain

# ── core.chat.message_elements ──
cce = _mod("core.chat.message_elements")


class Text:
    def __init__(self, text=""):
        self.text = text


class Forward:
    def __init__(self, chains=None, message_id=None, merge=True):
        self.chains = chains or []
        self.message_id = message_id
        self.merge = merge


cce.Text = Text
cce.Forward = Forward

# ── core.tag ──
ct = _mod("core.tag")


class _Tag:
    def __init__(self, name=""):
        self.name = name


class RootTagAction:
    def __init__(self, name="", value=None):
        self.tag = _Tag(name)
        self.value = value


ct.RootTagAction = RootTagAction


# ── fake context ──
class _MsgProcessor:
    def __init__(self):
        self.sent = []


class FakeAdapter:
    def __init__(self, name, platform, client):
        self.name = name
        self.info = types.SimpleNamespace(name=name, platform=platform)
        self.platform = platform
        self._client = client

    def get_client(self):
        return self._client


class _AdapterMgr:
    def __init__(self):
        self.adapters = {}

    def get_adapter(self, name):
        return self.adapters.get(name)


class FakeCtx:
    def __init__(self):
        self.adapter_mgr = _AdapterMgr()
        self.message_processor = _MsgProcessor()

    async def send_message_chain(self, sid, chain):
        self.message_processor.sent.append((sid, chain))


class FakeEvent:
    def __init__(self, sid="qq:gm:123", platform="QQ"):
        self.sid = sid
        self.adapter = types.SimpleNamespace(platform=platform, name=sid.split(":", 1)[0])
        self.session = types.SimpleNamespace(sid=sid)


def run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)
