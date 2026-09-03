# core/llm_reviewer.py
"""LLM 审查器：复用 AstrBot 已配置的 LLM provider，判定违规与入群申请。

- 每个群在 WebUI 选择 AstrBot 的聊天模型：主模型（llm_chat）、备用模型
  （llm_chat_fallback）、OCR 转述模型（llm_ocr_chat，需支持识图）
- 调用通过 self.context.llm_generate(chat_provider_id=..., prompt=..., contexts=...) 完成
- 图片消息审核：模型识图（modalities 含 image）则直接带图审核；否则先用 OCR 模型
  转述图片再按文本审核。主模型技术性失败自动切备用模型，备用模型按同样规则处理
- 内容风控拦截不切换（消息已被判敏感，切换无意义）
- 模型输出需为 JSON；解析失败按错误类型记录，供上层保守跳过或风险拦截判定
"""

from __future__ import annotations

import json
import re
from typing import Optional

from astrbot.api import logger

# 多模态消息结构（旧版 AstrBot 缺失时识图审核退化为纯文本）
try:
    from astrbot.core.agent.message import ImageURLPart, UserMessageSegment

    _MULTIMODAL_OK = True
except Exception:  # pragma: no cover
    _MULTIMODAL_OK = False

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

_OCR_PROMPT = (
    "你是图片内容审核助手。请仔细转述这张图片的全部内容：图中的文字（按原样转写）、"
    "画面场景、人物与物体、任何可疑或不当元素。只输出转述文本，不要任何前言或解释。"
)

_MAX_DESC_LEN = 800  # 图片转述文本截断长度，防止 prompt 超长
_MAX_IMAGES = 3  # 单条消息最多送审的图片数


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
    """通过 AstrBot 上下文调用已配置的聊天模型进行审核（支持图片消息）。"""

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

    # ------------------------------------------------------------------
    # 模型能力：是否支持识图（AstrBot provider 配置 modalities 含 image）
    # ------------------------------------------------------------------
    def model_supports_image(self, chat_id: str) -> bool:
        """查询 AstrBot 中该聊天模型是否声明支持图片输入（modalities 含 image）。"""
        if not chat_id or self.context is None or not _MULTIMODAL_OK:
            return False
        try:
            pm = getattr(self.context, "provider_manager", None)
            if pm is None:
                return False
            cfg = pm.get_provider_config_by_id(chat_id, merged=True)
            if isinstance(cfg, dict):
                mods = cfg.get("modalities") or []
                return "image" in [str(m).strip().lower() for m in mods]
        except Exception:
            return False
        return False

    # ------------------------------------------------------------------
    # OCR：用识图模型转述图片为文本
    # ------------------------------------------------------------------
    async def describe_images(self, image_urls: list, ocr_chat_id: str) -> Optional[str]:
        """用 OCR 模型转述图片，返回描述文本；未配置/失败返回 None。"""
        urls = [u for u in (image_urls or []) if str(u).startswith(("http://", "https://"))]
        if not urls or not ocr_chat_id or self.context is None or not _MULTIMODAL_OK:
            return None
        parts = [ImageURLPart(image_url=ImageURLPart.ImageURL(url=u)) for u in urls[:_MAX_IMAGES]]
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=ocr_chat_id,
                prompt=_OCR_PROMPT,
                contexts=[UserMessageSegment(content=parts)],
            )
            desc = str(getattr(resp, "completion_text", "") or "").strip()
            return desc[:_MAX_DESC_LEN] if desc else None
        except Exception as e:
            logger.warning(f"[LLMReviewer] 图片转述失败（OCR 模型 {ocr_chat_id}）: {e}")
            return None

    # ------------------------------------------------------------------
    # 核心调用：单模型生成 + JSON 解析（可带图）
    # ------------------------------------------------------------------
    async def _ask_one(
        self, chat_id: str, system: str, user: str, image_urls: list = None
    ) -> Optional[dict]:
        """对单个模型发起生成并解析 JSON 结果；模型识图且提供图片时直接带图审核。"""
        if not chat_id:
            self.last_error = "本群未选择 LLM 模型（llm_chat 为空）"
            self.last_error_type = "request_fail"
            return None
        if self.context is None:
            self.last_error = "AstrBot 运行上下文不可用"
            self.last_error_type = "request_fail"
            return None
        prompt = f"{system}\n\n{user}"
        # 带图条件：有图、框架支持多模态消息、且该模型声明识图（否则退化为纯文本）
        urls = None
        if image_urls and _MULTIMODAL_OK and self.model_supports_image(chat_id):
            urls = [u for u in image_urls if str(u).startswith(("http://", "https://"))][:_MAX_IMAGES] or None
        try:
            kwargs = {"chat_provider_id": chat_id, "prompt": prompt}
            if urls:
                parts = [ImageURLPart(image_url=ImageURLPart.ImageURL(url=u)) for u in urls]
                kwargs["contexts"] = [UserMessageSegment(content=parts)]
            resp = await self.context.llm_generate(**kwargs)
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

    # ------------------------------------------------------------------
    # 主/备调用链：主模型技术性失败切备用；两阶段处理图片（识图直审 / OCR 转述再审）
    # ------------------------------------------------------------------
    async def _ask(
        self,
        chat_id: str,
        fallback_chat_id: str,
        system: str,
        user: str,
        image_urls: list = None,
        ocr_chat_id: str = "",
    ) -> Optional[dict]:
        """主模型审核；技术性失败自动切备用模型。图片按各模型识图能力分别处理。"""
        image_urls = list(image_urls or [])

        # 主模型阶段：不识图且有图 → 先 OCR 转述，转述文本拼入审核输入
        desc = None
        main_user = user
        main_urls = image_urls
        if image_urls and not self.model_supports_image(chat_id):
            main_urls = None  # 主模型吃不了图，走转述文本
            desc = await self.describe_images(image_urls, ocr_chat_id)
            if desc:
                main_user = f"{user}\n[图片内容转述] {desc}"
            elif user.strip():
                main_user = f"{user}\n[消息含 {len(image_urls)} 张图片，未能转述]"

        result = await self._ask_one(chat_id, system, main_user, main_urls)
        if result is not None:
            return result

        # 备用模型阶段：仅技术性失败切换；风控拦截不切
        fb = (fallback_chat_id or "").strip()
        if fb and fb != chat_id and self.last_error_type != "risk_block":
            prior = self.last_error
            fb_user = user
            fb_urls = image_urls
            if image_urls and not self.model_supports_image(fb):
                # 兜底模型也不识图：复用/补做 OCR 转述
                fb_urls = None
                if desc is None:
                    desc = await self.describe_images(image_urls, ocr_chat_id)
                if desc:
                    fb_user = f"{user}\n[图片内容转述] {desc}"
                elif user.strip():
                    fb_user = f"{user}\n[消息含 {len(image_urls)} 张图片，未能转述]"
            elif image_urls and user.strip():
                # 兜底模型识图：带原文+图直接审
                pass
            result = await self._ask_one(fb, system, fb_user, fb_urls)
            if result is not None:
                logger.info(
                    f"[LLMReviewer] 主模型 {chat_id} 失败，已切换到备用 {fb}: {prior}"
                )
                self.last_error = ""
                self.last_error_type = ""
        return result

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    async def judge_message(
        self,
        sender: str,
        text: str,
        prompt: str = "",
        chat_id: str = "",
        fallback_chat_id: str = "",
        image_urls: list = None,
        ocr_chat_id: str = "",
    ) -> Optional[dict]:
        """判定一条群消息（可含图片）是否违规。违规时 allowed 为 false。

        prompt 为该群自定义审核要求（guard_prompt，由调用方传入），完全由用户定义、
        无内置默认话术；未填写时系统提示仅保留 JSON 输出格式约束。
        chat_id 为该群选用的 AstrBot 聊天模型，fallback_chat_id 为备用模型；
        image_urls 为消息中的图片，ocr_chat_id 为图片转述模型（主/兜底模型不识图时使用）。
        当模型输出触发风控特征时，返回带 source="risk_block" 的疑似违规判定；
        其余失败返回 None。
        """
        wanted = str(prompt or "").strip()
        # 不内置任何默认提示词：自定义要求非空时拼在格式约束前，为空则仅输出格式约束
        system = f"{wanted}\n{_JSON_RULE}" if wanted else _JSON_RULE
        content = text[:2000] if (text or "").strip() else "[纯图片消息]"
        user = f"发言者：{sender}\n消息内容：{content}"
        result = await self._ask(
            chat_id, fallback_chat_id, system, user,
            image_urls=image_urls, ocr_chat_id=ocr_chat_id,
        )
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