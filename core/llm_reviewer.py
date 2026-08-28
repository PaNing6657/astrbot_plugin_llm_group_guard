# core/llm_reviewer.py
"""OpenAI 兼容 LLM 审核器：判定群消息是否违规，支持多模型自动切换。

- 主模型：llm_base_url / llm_api_key / llm_model
- 备用模型：llm_fallback_base_urls / llm_fallback_api_keys / llm_fallback_models（按索引对应）
- 技术性错误（HTTP 失败/网络/空内容/解析失败）自动切换到下一个模型重试
- 内容风控拦截（risk_block）不切换：消息被服务端判定敏感，直接按疑似违规处理
"""

from __future__ import annotations

import json
import re
from typing import Optional

import aiohttp

from astrbot.api import logger

DEFAULT_BASE_URL = "https://api.openai.com/v1"

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
    """OpenAI 兼容接口的审核客户端（主模型 + 备用模型自动切换）。"""

    def __init__(self, config: dict):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_error: str = ""  # 最近一次审核失败的原因，供上层日志输出
        self.last_error_type: str = ""  # request_fail / http_error / risk_block / parse_fail / empty_content

    @property
    def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def enabled(self) -> bool:
        return bool(
            (self.config.get("llm_api_key") or "")
            and (self.config.get("llm_base_url") or "")
            and (self.config.get("llm_model") or "")
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _providers(self) -> list[tuple[str, str, str]]:
        """主模型 + 备用模型（按索引对齐，跳过不完整项），返回 [(base_url, api_key, model)]。"""
        providers: list[tuple[str, str, str]] = []
        base = (self.config.get("llm_base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        key = str(self.config.get("llm_api_key") or "").strip()
        model = str(self.config.get("llm_model") or "").strip()
        if base and key and model:
            providers.append((base, key, model))

        fb_base = self.config.get("llm_fallback_base_urls") or []
        fb_key = self.config.get("llm_fallback_api_keys") or []
        fb_model = self.config.get("llm_fallback_models") or []
        for i in range(max(len(fb_base), len(fb_key), len(fb_model))):
            b = str(fb_base[i]).strip().rstrip("/") if i < len(fb_base) else ""
            k = str(fb_key[i]).strip() if i < len(fb_key) else ""
            m = str(fb_model[i]).strip() if i < len(fb_model) else ""
            if b and k and m:
                providers.append((b, k, m))
        return providers

    async def _chat(self, system_prompt: str, user_content: str) -> Optional[dict]:
        providers = self._providers()
        if not providers:
            self.last_error = "未配置任何有效的 LLM（主模型与备用模型均不完整）"
            self.last_error_type = "request_fail"
            logger.error(f"[LLMReviewer] {self.last_error}")
            return None

        failures = []
        for index, (base, key, model) in enumerate(providers):
            result, err_type, err_msg = await self._try_provider(
                base, key, model, system_prompt, user_content
            )
            if result is not None:
                if index > 0:
                    logger.info(f"[LLMReviewer] 主模型失败，已切换到备用模型 #{index}（{model}）")
                self.last_error = ""
                self.last_error_type = ""
                return result
            failures.append(f"[{index}]{model}: {err_msg}")
            if err_type == "risk_block":
                # 内容风控拦截：消息被判定敏感，换模型无意义，直接停止
                self.last_error = err_msg
                self.last_error_type = "risk_block"
                return None
            # 技术性失败，尝试下一个模型
            logger.warning(f"[LLMReviewer] 模型 {model} 失败，尝试下一个: {err_msg}")

        self.last_error = "所有模型均失败：" + "；".join(failures)
        self.last_error_type = "request_fail"
        logger.error(f"[LLMReviewer] {self.last_error}")
        return None

    async def _try_provider(
        self, base: str, key: str, model: str, system_prompt: str, user_content: str
    ) -> tuple[Optional[dict], str, str]:
        """对单个模型发起请求并解析，返回 (result, error_type, error_message)。"""
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": int(self.config.get("llm_max_tokens") or 2000),
        }
        timeout = int(self.config.get("llm_timeout") or 30)
        try:
            async with self._s.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    return None, "http_error", f"HTTP {resp.status}: {body}"
                data = await resp.json()
        except Exception as exc:
            return None, "request_fail", f"请求失败: {exc}"

        try:
            message = data["choices"][0]["message"]
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError):
            return None, "http_error", f"响应结构异常: {str(data)[:300]}"
        if isinstance(content, str):
            if not content.strip():
                # 推理模型（如 deepseek-reasoner）常把输出放在 reasoning_content
                content = message.get("reasoning_content") or message.get("text") or ""
        else:
            # 部分服务可能返回 content 数组（如 [{"type":"text","text":"..."}]）
            try:
                content = content[0].get("text") or ""
            except (IndexError, AttributeError, TypeError):
                content = ""
        content = str(content).strip()
        if not content:
            finish_reason = ""
            try:
                finish_reason = str(data["choices"][0].get("finish_reason") or "")
            except (KeyError, IndexError, TypeError):
                pass
            if finish_reason == "length":
                return None, "empty_content", (
                    "模型输出被 max_tokens 截断（finish_reason=length，模型 "
                    f"{model} 可能把 token 消耗在思考上），请调大 llm_max_tokens 或改用非推理模型"
                )
            return None, "empty_content", (
                f"模型 {model} 返回空内容（可能是推理模型，输出不在 content 字段）。"
                f"原始响应: {str(data)[:200]}"
            )
        result = extract_json_object(content)
        if result is None:
            raw = str(content)[:200]
            if any(k in raw.lower() for k in _RISK_KEYWORDS):
                return None, "risk_block", f"服务端风控拦截了请求（可能因审核消息内容敏感）: {raw}"
            return None, "parse_fail", f"模型输出无法解析为 JSON: {raw}"
        return result, "", ""

    async def judge_message(self, sender: str, text: str) -> Optional[dict]:
        """判定一条群消息是否违规。违规时 allowed 为 false。

        当 LLM 服务端因内容风控拒绝请求时，返回带 source="risk_block" 的疑似违规
        判定（服务端拒绝本身即说明消息内容被判定为高风险）；其余失败返回 None。
        """
        prompt = (self.config.get("guard_prompt") or "").strip()
        if not prompt:
            prompt = "你是本群的 AI 管理员，请负责地判断群内消息是否存在违规行为。"
        system = f"{prompt}\n{_JSON_RULE}"
        user = f"发言者：{sender}\n消息内容：{text[:2000]}"
        result = await self._chat(system, user)
        if result is None and self.last_error_type == "risk_block":
            return {
                "allowed": False,
                "reason": "消息触发服务端内容风控，疑似违规",
                "source": "risk_block",
            }
        return result

    async def judge_join_request(self, comment: str) -> Optional[dict]:
        """判定入群申请信息是否同时包含【昵称】与【OID/UID】，返回结构化结果。

        失败返回 None（如 LLM 未配置或请求失败），由调用方保守处理。
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
        result = await self._chat(system, user)
        if result is None:
            return None
        return {
            "has_nickname": bool(result.get("has_nickname")),
            "has_oid": bool(result.get("has_oid")),
            "nickname": str(result.get("nickname") or "").strip(),
            "oid": str(result.get("oid") or "").strip(),
            "comment": str(result.get("comment") or "").strip(),
        }