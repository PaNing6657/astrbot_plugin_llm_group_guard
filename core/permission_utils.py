# permission_utils.py

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from typing import Tuple

from .event_utils import unwrap_event


async def check_group_and_permission(
    event: AiocqhttpMessageEvent,
    allow_groupadmin_use: bool,
    operator_name: str
) -> Tuple[bool, str | None]:
    """
    检查当前是否在群聊中，并验证操作者是否具有执行管理操作的权限。

    Returns:
        (has_permission: bool, error_message: str | None)
        - 如果有权限，返回 (True, None)
        - 如果无权限或不在群聊，返回 (False, "错误信息")
    """
    event = unwrap_event(event)
    group_id = event.get_group_id()
    self_id = event.get_self_id()
    if not group_id:
        return False, "此操作仅可在群聊中进行。"

    # 1. 机器人自身权限
    try:
        bot_member_info = await event.bot.get_group_member_info(
            group_id=group_id,
            user_id=self_id
        )
    except Exception as e:
        logger.error(f"[permission] 查询机器人身份失败: {e}")
        return False, f"权限校验失败：查询机器人身份出错（{e}）"
    bot_role = bot_member_info.get('role', 'member') if isinstance(bot_member_info, dict) else 'member'
    if bot_role not in ['owner', 'admin']:
        return False, f"机器人权限不足（当前身份「{bot_role}」），请确保机器人在群内具有群主或管理员权限。"

    # 2. 操作者权限
    operator_user_id = event.get_sender_id()
    has_permission = False

    if allow_groupadmin_use:
        try:
            group_member_info = await event.bot.get_group_member_info(
                group_id=group_id,
                user_id=operator_user_id
            )
        except Exception as e:
            logger.error(f"[permission] 查询操作者 {operator_user_id} 身份失败: {e}")
            group_member_info = {}
        role = group_member_info.get('role', 'member') if isinstance(group_member_info, dict) else 'member'
        if role in ['owner', 'admin']:
            has_permission = True
        else:
            logger.info(f"[permission] 操作者 {operator_user_id} 群内身份「{role}」，非群主/管理员")

    if not has_permission and event.is_admin():
        has_permission = True

    if not has_permission:
        return False, f"用户 {operator_name} 权限不足（不在 AstrBot 管理员列表，且未识别为群主/管理员）"

    return True, None