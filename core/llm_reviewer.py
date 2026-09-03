# core/llm_reviewer.py
"""LLM 审查器：复用 AstrBot 已配置的 LLM provider，判定违规与入群申请。

- 每个群在 WebUI 选择 AstrBot 的一个聊天模型（llm_chat，如 botcf/gpt-5.6-luna）
- 调用通过 self.context.llm_generate(chat_provider_id=..., prompt=...) 完成
- 模型输出需为 JSON；解析失败按错误类型记录，供上层保守跳过或风险拦截判定
"""

from __future__ import annotations

import json
import re
from typing import Optional

from astrbot.api import logger

_JSON_RULE = (
    "你必须严格只输出一个 JSON 对象，不要输出任何无关文字、注释或 Markdown 代码块。"
    '字段：{"allowed": true/false, "reason": "简短的中文原因"}'
)

_JOIN_JSON_RULE = (
    "你必须严格只输出一个 JSON 对象，不要输出任何无关文字、注释或 Markdown 代码块。"
    '字段：{"has_nickname": true/false, "has_oid": true/false, "nickname": "申请信息中的昵称（无则为空字符串）", '
    '"oid": "申请信息中的OID/UID值（无则为空字符串）", "comment": "简短中文说明信息是否完整或缺失了什么"}'
)

_RISK_KEYWORDS = (
    "high risk",
    "rejected",
    "content policy",
    "sensitive",
    "unsafe",
    "risk control",
    "风控",
    "敏感",
    "拒绝",
)


def extract_json_object(content: str) -> Optional[dict]:
    """从模型输出中提取 JSON 对象，容忍包裹的代码块与前后杂讯。"""
    if not content:
        return None
    text = str(content).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1]):
        if not candidate or not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue
    return None


class LLMReviewer:
    """通过 AstrBot 上下文调用已配置的聊天模型进行审核。"""

    def __init__(self, config: dict, context=None):
        self.config = config
        self.context = context
        self.last_error: str = ""  # 最近一次审核失败的原因，供上层日志输出
        self.last_error_type: str = ""  # request_fail / risk_block / parse_fail / empty_content

    def enabled(self) -> bool:
        """是否具备调用能力：AstrBot 运行上下文可用（所选模型是否有效由调用方 chat_id 决定）。"""
        return self.context is not None

    async def close(self) -> None:
        """无需自持连接，无需清理。"""

    async def _ask(
        self, chat_id: str, fallback_chat_id: str, system: str, user: str
    ) -> Optional[dict]:
        """调用 AstrBot 指定聊天模型并解析 JSON 结果；主模型技术性失败时自动切到备用模型。"""
        result = await self._ask_one(chat_id, system, user)
        if result is not None:
            return result
        # 主模型失败：技术性故障切备用；内容风控（消息被判敏感）切换无意义，不切
        fb = (fallback_chat_id or "").strip()
        if fb and fb != chat_id and self.last_error_type != "risk_block":
            prior = self.last_error
            result = await self._ask_one(fb, system, user)
            if result is not None:
                logger.info(
                    f"[LLMReviewer] 主模型 {chat_id} 失败，已切换到备用 {fb}: {prior}"
                )
                # 已成功，清空旧错误
                self.last_error = ""
                self.last_error_type = ""
        return result

    async def _ask_one(self, chat_id: str, system: str, user: str) -> Optional[dict]:
        """对单个模型发起生成并解析 JSON 结果。"""
        if not chat_id:
            self.last_error = "本群未选择 LLM 模型（llm_chat 为空）"
            self.last_error_type = "request_fail"
            return None
        if self.context is None:
            self.last_error = "AstrBot 运行上下文不可用"
            self.last_error_type = "request_fail"
            return None
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=chat_id, prompt=f"{system}\n\n{user}"
            )
            content = getattr(resp, "completion_text", "") or ""
        except Exception as e:
            self.last_error = f"调用 {chat_id} 失败: {e}"
            self.last_error_type = "request_fail"
            logger.error(f"[LLMReviewer] {self.last_error}")
            return None
        content = str(content).strip()
        if not content:
            self.last_error = f"模型 {chat_id} 返回空内容"
            self.last_error_type = "empty_content"
            logger.error(f"[LLMReviewer] {self.last_error}")
            return None
        result = extract_json_object(content)
        if result is None:
            raw = content[:200]
            if any(k in raw.lower() for k in _RISK_KEYWORDS):
                self.last_error = f"服务端风控拦截了请求（可能因审核消息内容敏感）: {raw}"
                self.last_error_type = "risk_block"
                return None
            self.last_error = f"模型输出无法解析为 JSON: {raw}"
            self.last_error_type = "parse_fail"
            return None
        return result

    async def judge_message(
        self,
        sender: str,
        text: str,
        prompt: str = "",
        chat_id: str = "",
        fallback_chat_id: str = "",
    ) -> Optional[dict]:
        """判定一条群消息是否违规。违规时 allowed 为 false。

        prompt 为该群审核要求（guard_prompt，由调用方传入）；chat_id 为该群选用的
        AstrBot 聊天模型，fallback_chat_id 为备用模型（主模型技术性失败时自动切换）。
        当模型输出触发风控特征时，返回带 source="risk_block" 的疑似违规判定
        （消息内容疑似敏感）；其余失败返回 None。
        """
        wanted = str(prompt or "").strip()
        if not wanted:
            wanted = "你是本群的 AI 管理员，请负责地判断群内消息是否存在违规行为。"
        system = f"{wanted}\n{_JSON_RULE}"
        user = f"发言者：{sender}\n消息内容：{text[:2000]}"
        result = await self._ask(chat_id, fallback_chat_id, system, user)
        if result is None and self.last_error_type == "risk_block":
            return {
                "allowed": False,
                "reason": "消息触发内容风控，疑似违规",
                "source": "risk_block",
            }
        return result

    async def judge_join_request(
        self, comment: str, chat_id: str = "", fallback_chat_id: str = ""
    ) -> Optional[dict]:
        """判定入群申请信息是否同时包含【昵称】与【OID/UID】，返回结构化结果。

        chat_id 为该群选用的 AstrBot 聊天模型；失败返回 None（如未配置模型或调用失败），
        由调用方保守处理。
        """
        if not comment.strip():
            return {
                "has_nickname": False,
                "has_oid": False,
                "nickname": "",
                "oid": "",
                "comment": "申请信息为空",
            }
        prompt = (
            "你是入群申请审核助手。请检查申请人填写的入群验证信息："
            "它必须同时包含【昵称】和【OID】（也称 UID，是一串纯数字编号，如 QQ 号、学号等，不含字母）。"
            "信息模糊、可读性差或格式不符合要求时倾向保守判断。"
        )
        system = f"{prompt}\n{_JOIN_JSON_RULE}"
        user = f"入群验证信息内容：\n{comment[:500]}"
        result = await self._ask(chat_id, fallback_chat_id, system, user)
        if result is None:
            return None
        return {
            "has_nickname": bool(result.get("has_nickname")),
            "has_oid": bool(result.get("has_oid")),
            "nickname": str(result.get("nickname") or "").strip(),
            "oid": str(result.get("oid") or "").strip(),
            "comment": str(result.get("comment") or "").strip(),
        }