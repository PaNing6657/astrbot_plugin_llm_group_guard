# core/llm_reviewer.py
"""LLM 审查器：复用 AstrBot 已配置的 LLM provider，判定违规与入群申请。

- 每个群在 WebUI 选择 AstrBot 的聊天模型（llm_chat，如 botcf/gpt-5.6-luna）
- 文本审核通过 self.context.llm_generate(chat_provider_id=..., prompt=...) 完成
- 支持识图的审核模型可“直接看图”：图片消息把图片连同审核指令一起交给该模型
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

    @staticmethod
    def provider_supports_vision(provider) -> bool:
        """探测 provider 是否支持识图：优先读添加模型时设置的 modalities（模态）配置。"""
        meta = None
        try:
            meta = provider.meta()
        except Exception:
            pass
        # 模态列表/字符串：包含 图片/image/vision 即视为支持识图
        modal_sources = []
        if meta is not None:
            modal_sources.append(getattr(meta, "modalities", None))
        modal_sources.append(getattr(provider, "config", {}).get("modalities", None) if hasattr(provider, "config") else None)
        for source in modal_sources:
            if isinstance(source, list):
                if any(str(m).strip().lower() in ("image", "vision", "img", "图片") for m in source):
                    return True
            elif isinstance(source, str) and source.strip():
                if any(k in source.lower() for k in ("image", "vision", "图片")):
                    return True
        # 兜底传统字段
        try:
            if meta is not None:
                return bool(
                    getattr(meta, "vision", None)
                    or getattr(meta, "multimodal", None)
                    or getattr(meta, "supports_vision", None)
                )
        except Exception:
            pass
        return False

    def supports_vision(self, chat_id: str) -> bool:
        """指定模型是否支持识图（可直接看图）。"""
        if not chat_id or self.context is None:
            return False
        try:
            provider = self.context.get_provider_by_id(chat_id)
            return provider is not None and self.provider_supports_vision(provider)
        except Exception:
            return False

    async def close(self) -> None:
        """无需自持连接，无需清理。"""

    @staticmethod
    def _resp_text(resp) -> str:
        """兼容不同模型返回形态：str / tuple(str,...) / LLMResponse / dict。"""
        if isinstance(resp, (tuple, list)):
            resp = resp[0] if resp else ""
        elif hasattr(resp, "completion_text"):
            resp = resp.completion_text
        elif hasattr(resp, "get"):
            resp = resp.get("completion_text") or resp.get("text") or ""
        return str(resp or "").strip()

    @staticmethod
    async def _provider_text_chat(provider, prompt: str, image_urls) -> str:
        """调用 provider 的 text_chat，兼容关键字/位置参数，返回文本。"""
        try:
            resp = await provider.text_chat(prompt=prompt, image_urls=list(image_urls))
        except TypeError as e:
            logger.debug(f"[LLMReviewer] text_chat 关键字参数不受支持，改用位置参数: {e}")
            resp = await provider.text_chat(prompt, list(image_urls))
        return LLMReviewer._resp_text(resp)

    async def _ask(
        self,
        chat_id: str,
        fallback_chat_id: str,
        system: str,
        user: str,
        image_urls: Optional[list] = None,
    ) -> Optional[dict]:
        """调用模型并解析 JSON；主模型技术性失败时自动切到备用模型。"""
        result = await self._ask_one(chat_id, system, user, image_urls)
        if result is not None:
            return result
        # 主模型失败：技术性故障切备用；内容风控（消息被判敏感）切换无意义，不切
        fb = (fallback_chat_id or "").strip()
        if fb and fb != chat_id and self.last_error_type != "risk_block":
            prior = self.last_error
            result = await self._ask_one(fb, system, user, image_urls)
            if result is not None:
                logger.info(
                    f"[LLMReviewer] 主模型 {chat_id} 失败，已切换到备用 {fb}: {prior}"
                )
                # 已成功，清空旧错误
                self.last_error = ""
                self.last_error_type = ""
        return result

    async def _ask_one(
        self,
        chat_id: str,
        system: str,
        user: str,
        image_urls: Optional[list] = None,
    ) -> Optional[dict]:
        """对单个模型发起生成并解析 JSON 结果；带图且模型支持识图时直接传图。"""
        if not chat_id:
            self.last_error = "本群未选择 LLM 模型（llm_chat 为空）"
            self.last_error_type = "request_fail"
            return None
        if self.context is None:
            self.last_error = "AstrBot 运行上下文不可用"
            self.last_error_type = "request_fail"
            return None
        prompt = f"{system}\n\n{user}"
        if image_urls:
            if not self.supports_vision(chat_id):
                self.last_error = f"模型 {chat_id} 不支持识图，无法直接审核图片"
                self.last_error_type = "request_fail"
                logger.warning(f"[LLMReviewer] {self.last_error}")
                return None
            try:
                provider = self.context.get_provider_by_id(chat_id)
                if provider is None:
                    self.last_error = f"模型 {chat_id} 不可用"
                    self.last_error_type = "request_fail"
                    return None
                content = await self._provider_text_chat(provider, prompt, image_urls)
            except Exception as e:
                self.last_error = f"调用 {chat_id} 审核图片失败: {e}"
                self.last_error_type = "request_fail"
                logger.error(f"[LLMReviewer] {self.last_error}")
                return None
        else:
            try:
                resp = await self.context.llm_generate(chat_provider_id=chat_id, prompt=prompt)
                content = self._resp_text(resp)
            except Exception as e:
                self.last_error = f"调用 {chat_id} 失败: {e}"
                self.last_error_type = "request_fail"
                logger.error(f"[LLMReviewer] {self.last_error}")
                return None
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

    async def describe_image(self, image_url: str, ocr_chat_id: str) -> str:
        """用支持识图的多模态模型转述图片内容（文字/画面），失败返回空字符串。"""
        if not ocr_chat_id or self.context is None:
            return ""
        try:
            provider = self.context.get_provider_by_id(ocr_chat_id)
            if provider is None:
                self.last_error = f"识图模型 {ocr_chat_id} 不可用"
                logger.warning(f"[LLMReviewer] {self.last_error}")
                return ""
            content = await self._provider_text_chat(
                provider,
                "请识别并完整转录这张图片中的文字内容；若图片不含文字，请简要描述图片画面。",
                [image_url],
            )
        except Exception as e:
            self.last_error = f"调用识图模型 {ocr_chat_id} 失败: {e}"
            logger.warning(f"[LLMReviewer] {self.last_error}")
            return ""
        return content

    async def judge_message(
        self,
        sender: str,
        text: str,
        prompt: str = "",
        chat_id: str = "",
        fallback_chat_id: str = "",
        extra_context: str = "",
        image_urls: Optional[list] = None,
    ) -> Optional[dict]:
        """判定一条群消息是否违规。违规时 allowed 为 false。

        prompt 为该群自定义审核要求（guard_prompt，由调用方传入），完全由用户定义、
        无内置默认话术；未填写时系统提示仅保留 JSON 输出格式约束。
        chat_id 为该群选用的 AstrBot 聊天模型，fallback_chat_id 为备用模型
        （主模型技术性失败时自动切换）。extra_context 为图片转述文本
        （模型不支持识图时由识图模型转入）；image_urls 为消息附带的图片 URL
        （模型支持识图时直接传图审核）。当模型输出触发风控特征时，返回带
        source="risk_block" 的疑似违规判定（消息内容疑似敏感）；其余失败返回 None。
        """
        wanted = str(prompt or "").strip()
        # 不内置任何默认提示词：自定义要求非空时拼在格式约束前，为空则仅输出格式约束
        system = f"{wanted}\n{_JSON_RULE}" if wanted else _JSON_RULE
        user = f"发言者：{sender}\n消息内容：{text[:2000]}"
        if extra_context:
            user += f"\n该消息附带图片的转述内容：\n{extra_context[:1500]}"
        result = await self._ask(chat_id, fallback_chat_id, system, user, image_urls)
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