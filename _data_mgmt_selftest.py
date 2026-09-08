# 自测：数据管理（读取本地数据概览 + 一键删除指定群 + 清空）
import json
import os
import sys
import tempfile
import types

# ---- stub astrbot ----
pkg = types.ModuleType("astrbot")
sys.modules["astrbot"] = pkg
api = types.ModuleType("astrbot.api")
api.AstrBotConfig = dict
api.logger = types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None,
                                   error=lambda *a: None, debug=lambda *a: None)
sys.modules["astrbot.api"] = api
ev = types.ModuleType("astrbot.api.event")
ev.AstrMessageEvent = object
_identity = lambda f: f
_deco = lambda *a, **k: _identity
ev.filter = types.SimpleNamespace(platform_adapter_type=_deco, command=_deco,
                                  event_message_type=_deco, llm_tool=_deco, on_llm_request=_deco,
                                  PlatformAdapterType=types.SimpleNamespace(AIOCQHTTP="aiocqhttp"),
                                  EventMessageType=types.SimpleNamespace(GROUP_MESSAGE="g", ALL="all"))
sys.modules["astrbot.api.event"] = ev
star = types.ModuleType("astrbot.api.star")
star.Context = object
star.Star = object
star.register = lambda *a, **k: (lambda c: c)
sys.modules["astrbot.api.star"] = star
web = types.ModuleType("astrbot.api.web")
web.error_response = lambda m: {"error": m}
web.json_response = lambda d: d
web.request = types.SimpleNamespace(json=lambda default: default,
                                    query=types.SimpleNamespace(get=lambda k, d=None: d))
sys.modules["astrbot.api.web"] = web
acqev = types.ModuleType("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event")
acqev.AiocqhttpMessageEvent = object
sys.modules["astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"] = acqev
# core.whole_ban_scheduler / violation_tracker 无 astrbot 深层依赖，直接真实导入

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import astrbot_plugin_llm_group_guard.main as main
from astrbot_plugin_llm_group_guard.core.whole_ban_scheduler import WholeBanScheduler

tmpdir = tempfile.mkdtemp()


class FakeGuard:
    def __init__(self):
        self.violation_tracker = FakeTracker()
        self.keyword_minor_tracker = FakeTracker()
        self.keyword_major_tracker = FakeTracker()
        self.violation_log = FakeLog()


class FakeTracker:
    def __init__(self):
        self.counts = {"101": {"42": 3}, "202": {"7": 1}}
        self.saved = []

    def save(self):
        self.saved.append(1)


class FakeLog:
    def __init__(self):
        self.entries = [
            {"gid": "101", "uid": "42", "text": "x", "reason": "y", "source": "llm", "ts": 1},
            {"gid": "202", "uid": "7", "text": "z", "reason": "w", "source": "keyword_minor", "ts": 2},
        ]

    def clear(self, gid=None, user_id=None):
        if gid is None and user_id is None:
            self.entries = []
        else:
            self.entries = [e for e in self.entries if not (gid is None or e.get("gid") == str(gid))]


def make_inst(seed):
    inst = object.__new__(main.LLMGroupGuardPlugin)
    inst.config = {
        "global": {},
        "groups": {"101": {"guard_enable": True}, "202": {"guard_enable": False}},
    }
    inst.data_dir = tmpdir
    inst._config_path = os.path.join(tmpdir, "config.json")
    inst._group_template = {}
    # scheduler：真实调度器，101 有每日任务
    inst.scheduler = WholeBanScheduler(tmpdir)
    inst.scheduler.set("101", 1000, 2000, recurring=True)
    inst.scheduler.set("303", 3000, 4000)  # 只有任务、无配置的群
    inst.guard = FakeGuard()
    inst._group_runtime = {"101": {"bot": "x"}}
    inst._join_oid = {"101": {"42": "oid"}}
    return inst


# 1. 概览：合并 config/schedule/violations/log 四类来源
inst = make_inst("a")
idx = inst._local_data_index()
assert set(idx) == {"101", "202", "303"}, idx
assert idx["101"] == {"config": True, "schedule": True, "violations": True, "log": True}
assert idx["202"] == {"config": True, "violations": True, "log": True}
assert idx["303"] == {"schedule": True}

# 2. 删除指定群：101 全部数据清掉，内存+磁盘同步
inst._purge_group_data("101")
idx2 = inst._local_data_index()
assert "101" not in idx2, idx2
assert "101" not in inst.config["groups"]
assert "101" not in inst.scheduler.all()
assert all("101" not in t.counts for t in (inst.guard.violation_tracker,
                                           inst.guard.keyword_minor_tracker,
                                           inst.guard.keyword_major_tracker))
assert all(e["gid"] != "101" for e in inst.guard.violation_log.entries)
assert "101" not in inst._group_runtime and "101" not in inst._join_oid
# 其他群不受影响
assert "202" in idx2 and "303" in idx2

# 3. 清空全部
inst._purge_group_data("202")
inst._purge_group_data("303")
idx3 = inst._local_data_index()
assert idx3 == {}, idx3
assert inst.config["groups"] == {}
assert inst.scheduler.all() == {}
assert inst.guard.violation_log.entries == []

# 4. 定时禁言文件落盘后不再包含已删群
with open(os.path.join(tmpdir, "schedule_ban.json"), encoding="utf-8") as f:
    disk = json.load(f)
assert "101" not in disk and "303" not in disk, disk

print("数据管理自测全部通过")