# core/text_utils.py
"""提示文本模板工具：把 {at_user} 等占位符编译为可直接发送的文本（含 CQ 码 @）。

入群审批/进群欢迎/改名片提示与违规通知共用同一套 @ 逻辑，
保证 OneBot 侧按 [CQ:at,qq=...] 解析为真正的 @（显示成员当前的群昵称），
而不是把 QQ 号当普通文本发出去。
"""

from __future__ import annotations

import re
from typing import Mapping

# 手写的 "@{user_id}" / "@ {user_id}"（旧配置常见写法）只会发出纯文本 QQ 号，
# 这里统一升级为真正的 @，避免"@ 的是 QQ 号而不是昵称"。
_AT_ALIAS_RE = re.compile(r"@\s*\{(?:user_id|at_user)\}")


def build_text_with_at(template: str, vars_map: Mapping[str, object], user_id: object) -> str:
    """把模板编译为文本：占位符按 vars_map 替换，{at_user} 替换成 CQ 码 @ 该用户（可多次出现）。

    手写的 "@{user_id}" 也按 @ 处理（旧配置无需改动即可正确 @ 到昵称）。
    """
    text = _AT_ALIAS_RE.sub("{at_user}", str(template))
    for k, v in (vars_map or {}).items():
        text = text.replace(k, str(v))
    return text.replace("{at_user}", f"[CQ:at,qq={user_id}]")
