# core/llm_reviewer.py
"""LLM 审查器：复用 AstrBot 已配置的 LLM provider，判定违规与入群申请。

- 每个群在 WebUI 选择 AstrBot 的聊天模型：主模型（llm_chat）、备用模型
  （llm_chat_fallback）、识图审核模型（llm_ocr_chat，需支持识图）
- 入群审批可另选独立模型（join_llm_chat / join_llm_chat_fallback / join_llm_ocr_chat），
  未选择时由调用方回退到消息审核模型；审批要求完全自定义（join_prompt）
- 调用通过 self.context.llm_generate(chat_provider_id=..., prompt=..., contexts=...) 完成；
  主/备用模型选择内置的「D1 决策模型」(d1) 时改走 Liquid d1 的 System One 接口，
  由模型直接给出「是否违规」的概率判定（不生成文字，更快、更省）
- 图片消息审核：审核模型识图（modalities 含 image）则直接带图审核；不识图时由
  识图审核模型直接带图出判定（不转述）。主模型技术性失败自动切备用模型，
  备用模型按同样规则处理
- 内容风控拦截不切换（消息已被判敏感，切换无意义）
- 模型输出需为 JSON；解析失败按错误类型记录，供上层保守跳过或风险拦截判定
"""

from __future__ import annotations

import asyncio
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
    '字段：{"allowed": true/false（是否满足上述入群要求）, '
    '"has_nickname": true/false（申请信息中是否含昵称）, '
    '"has_oid": true/false（申请信息中是否含 OID/UID，即纯数字编号）, '
    '"nickname": "申请信息中的昵称（无则为空字符串）", '
    '"oid": "申请信息中的OID/UID值（无则为空字符串）", '
    '"reason": "不满足要求时的简短中文原因（满足要求时可为空字符串）", '
    '"comment": "简短中文说明信息是否完整或缺失了什么"}'
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

_MAX_IMAGES = 3  # 单条消息最多送审的图片数
_MAX_MSG_CHARS = 2000  # 单条消息最多送审的字数
_MAX_BATCH_CHARS = 8000  # 合并审核时整批消息正文的长度上限

# D1 决策模型（LiquidAI）：通过 System One 接口做「是/否」概率判定，不生成文字。
# 群配置里主/备用模型选择该特殊 ID 时走 HTTP 调用，其余模型仍走 AstrBot provider。
D1_CHAT_ID = "d1"
_D1_DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/systemone"
_D1_DEFAULT_MODEL = "liquid/d1"


def is_d1_model(chat_id: str) -> bool:
    """是否为 D1 决策模型（兼容 d1 与 Liquid 免费档 d1:free）。"""
    value = str(chat_id or "").strip().lower()
    return value == D1_CHAT_ID or value.startswith(D1_CHAT_ID + ":")


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
        """是否具备调用能力：AstrBot 运行上下文可用，或已配置 D1 API Key。"""
        return self.context is not None or bool(self._d1_settings()["api_key"])

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
    # D1 决策模型（LiquidAI System One 接口）：概率判定，不生成文字
    # ------------------------------------------------------------------
    @staticmethod
    def _d1_levels(value) -> list:
        """严重度档位：支持数组或每行一档的文本；完全由用户填写，无内置档位。"""
        if isinstance(value, str):
            parts = re.split(r"[\n,，]", value)
        elif isinstance(value, (list, tuple)):
            parts = [str(x) for x in value]
        else:
            parts = []
        return [str(p).strip() for p in parts if str(p).strip()][:10]

    def _d1_settings(self) -> dict:
        """读取全局 D1 配置（实时读取，WebUI 保存后立即生效）。"""
        try:
            raw = (self.config or {}).get("global", {}).get("d1") or {}
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}

        def _num(key: str, default: float, low: float, high: float) -> float:
            try:
                value = float(raw.get(key, default))
            except (TypeError, ValueError):
                value = float(default)
            return min(max(value, low), high)

        judge_mode = str(raw.get("judge_mode") or "noul").strip().lower()
        if judge_mode not in ("noul", "score"):
            judge_mode = "noul"
        levels = self._d1_levels(raw.get("severity_levels"))
        max_score = float(len(levels) - 1) if len(levels) >= 2 else 0.0
        return {
            "api_key": str(raw.get("api_key") or "").strip(),
            "endpoint": str(raw.get("endpoint") or _D1_DEFAULT_ENDPOINT).strip(),
            "model": str(raw.get("model") or _D1_DEFAULT_MODEL).strip(),
            "threshold": _num("threshold", 0.7, 0.05, 0.99),
            "timeout": _num("timeout", 30.0, 5.0, 120.0),
            "uncertain_as_violation": bool(raw.get("uncertain_as_violation")),
            "judge_mode": judge_mode,
            "severity_levels": levels,
            "score_threshold": _num("score_threshold", 1.5, 0.1, max(max_score - 0.05, 0.1)),
            # 免费档/高峰期限流（429）时等待该秒数后重试一次；0=不重试
            "retry_delay": _num("retry_delay", 2.0, 0.0, 10.0),
        }

    @staticmethod
    def _d1_instruction(system: str) -> str:
        """把聊天模型的 system 提示还原为判断题说明（去掉 JSON 输出约束）。"""
        text = str(system or "")
        for rule in (_JSON_RULE, _JOIN_JSON_RULE):
            text = text.replace(rule, "")
        return text.strip()  # 无内置兜底：审核要求为空时由调用方跳过审核

    @staticmethod
    async def _http_post_json(url: str, api_key: str, payload: dict, timeout: float):
        """POST JSON（返回 status, data）：优先 aiohttp，缺失时回退 urllib 线程调用。"""
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            import aiohttp  # type: ignore

            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    body = await resp.text()
                    try:
                        return resp.status, json.loads(body)
                    except (TypeError, ValueError):
                        return resp.status, None
        except ImportError:
            pass

        def _blocking():
            import urllib.error
            import urllib.request

            request = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read().decode("utf-8"))
                except (TypeError, ValueError):
                    return e.code, None

        return await asyncio.to_thread(_blocking)

    async def _ask_d1(
        self, user: str, instruction: str, need_severity: bool = False
    ) -> Optional[dict]:
        """D1 判定：消息作为 state、审核要求作为判断题，按概率或严重度给结论。

        判定标准完全来自用户配置（审核要求 + 严重度档位），没有任何内置兜底：
        未填写审核要求、或需要严重度但未填写档位时，直接跳过本次审核。
        按需提问：每个问题都会把 state 重新计一遍输入 token，所以只问这次真正要用的。
        - 判定依据=概率 且 该群未开启「按严重程度决定处置」→ 只问违规概率（Noul）
        - 判定依据=概率 且 开启了严重度处置 → 违规概率 + 严重度（Noul + Score）
        - 判定依据=严重度 → 只问严重度（Score）
        """
        cfg = self._d1_settings()
        if not cfg["api_key"]:
            self.last_error = "未配置 D1 API Key（WebUI 配置页 → D1 决策模型）"
            self.last_error_type = "request_fail"
            return None
        instruction = str(instruction or "").strip()
        if not instruction:
            self.last_error = "未填写审核要求（guard_prompt / 高召回审核要求），已跳过 D1 审核"
            self.last_error_type = "config_missing"
            logger.warning(f"[LLMReviewer] {self.last_error}")
            return None
        levels = cfg["severity_levels"]
        need_score = cfg["judge_mode"] == "score" or need_severity
        if need_score and len(levels) < 2:
            self.last_error = "未填写 D1 严重度档位（至少 2 档），已跳过审核"
            self.last_error_type = "config_missing"
            logger.warning(f"[LLMReviewer] {self.last_error}")
            return None
        questions: dict = {}
        if cfg["judge_mode"] != "score":
            questions["violate"] = {"type": "noul", "instructions": instruction}
        if need_score:
            # 每个问题独立评估：严重度问题同样要带上用户填写的判定要求，不能依赖另一个问题
            questions["severity"] = {
                "type": "score",
                "instructions": f"判定要求：\n{instruction}\n\n这段内容违反上述要求的严重程度如何？",
                "criteria": levels,
            }
        payload = {"model": cfg["model"], "state": user, "questions": questions}
        try:
            status, data = await self._http_post_json(
                cfg["endpoint"], cfg["api_key"], payload, cfg["timeout"]
            )
            if status == 429 and cfg["retry_delay"] > 0:
                # 限流（免费档常见）：等待片刻重试一次，避免直接判失败切备用模型
                logger.warning(
                    f"[LLMReviewer] D1 触发限流（429），{cfg['retry_delay']:.1f}s 后重试一次"
                )
                await asyncio.sleep(cfg["retry_delay"])
                status, data = await self._http_post_json(
                    cfg["endpoint"], cfg["api_key"], payload, cfg["timeout"]
                )
        except Exception as e:
            self.last_error = f"D1 请求失败: {e}"
            self.last_error_type = "request_fail"
            logger.error(f"[LLMReviewer] {self.last_error}")
            return None
        if status != 200:
            message = ""
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    message = str(err.get("message") or "")
                elif err:
                    message = str(err)
            self.last_error = f"D1 接口返回 HTTP {status}{'：' + message[:200] if message else ''}"
            self.last_error_type = "request_fail"
            logger.error(f"[LLMReviewer] {self.last_error}")
            return None
        answers = (data or {}).get("answers") or {}
        raw_prob = (answers.get("violate") or {}).get("noul")
        raw_score = (answers.get("severity") or {}).get("score")
        probability = float(raw_prob) if isinstance(raw_prob, (int, float)) else None
        severity = float(raw_score) if isinstance(raw_score, (int, float)) else None
        if probability is None and severity is None:
            self.last_error = f"D1 响应缺少 noul/score 结果: {str(data)[:200]}"
            self.last_error_type = "parse_fail"
            return None

        threshold = float(cfg["threshold"])
        score_threshold = float(cfg["score_threshold"])
        judge_mode = cfg["judge_mode"]
        if judge_mode == "score" and severity is not None:
            # 按严重程度判断：严重度 ≥ 阈值即判违规（无概率不确定区间）
            violated = severity >= score_threshold
        elif probability is not None:
            if judge_mode == "score":
                logger.warning("[LLMReviewer] D1 未返回严重度，已退回按违规概率判定")
            if probability >= threshold:
                violated = True
            elif probability <= 1 - threshold:
                violated = False
            else:
                violated = bool(cfg["uncertain_as_violation"])
        else:
            violated = severity >= score_threshold

        max_score = len(levels) - 1
        detail = []
        if severity is not None:
            detail.append(f"严重度 {severity:.2f}/{max_score}")
        if probability is not None:
            detail.append(f"违规概率 {probability * 100:.0f}%")
        usage = (data or {}).get("usage") or {}
        logger.info(
            f"[LLMReviewer] D1 判定（按{'严重程度' if judge_mode == 'score' else '违规概率'}）: "
            f"{'，'.join(detail) or '无结果'} → {'违规' if violated else '合规'}，"
            f"输入 {usage.get('input_tokens', '?')} tokens"
        )
        reason = ""
        if violated:
            extra = f"严重度 {severity:.1f}/{max_score}" if severity is not None else ""
            if probability is not None:
                prob_text = f"命中概率 {probability * 100:.0f}%"
                extra = f"{extra}，{prob_text}" if extra else prob_text
            reason = f"D1 判定违规（{extra}）" if extra else "D1 判定违规"
        return {
            "allowed": not violated,
            "reason": reason,
            "source": "d1",
            "probability": round(probability, 6) if probability is not None else None,
            "severity": round(severity, 4) if severity is not None else None,
            "severity_max": max_score,
        }

    # ------------------------------------------------------------------
    # 核心调用：单模型生成 + JSON 解析（可带图）
    # ------------------------------------------------------------------
    async def _ask_one(
        self, chat_id: str, system: str, user: str, image_urls: list = None,
        force_vision: bool = False, need_severity: bool = False,
    ) -> Optional[dict]:
        """对单个模型发起生成并解析 JSON 结果；识图模型直接带图审核。

        force_vision=True 时跳过 modalities 声明检查（用于用户指定的识图审核模型）。
        """
        if not chat_id:
            self.last_error = "本群未选择 LLM 模型（llm_chat 为空）"
            self.last_error_type = "request_fail"
            return None
        if is_d1_model(chat_id):
            # D1 决策模型：不生成文字，直接返回「是否违规」的判定
            return await self._ask_d1(user, self._d1_instruction(system), need_severity)
        if self.context is None:
            self.last_error = "AstrBot 运行上下文不可用"
            self.last_error_type = "request_fail"
            return None
        prompt = f"{system}\n\n{user}"
        # 带图条件：有图、框架支持多模态消息；模型声明识图或为指定的识图审核模型
        urls = None
        if image_urls and _MULTIMODAL_OK and (force_vision or self.model_supports_image(chat_id)):
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
    # 主/备调用链：审核模型不识图 → 识图审核模型直接带图出判定；技术性失败切备用
    # ------------------------------------------------------------------
    @staticmethod
    def _text_only_user(user: str, image_count: int) -> str:
        """降级纯文本审核时补充图片数量说明。"""
        if image_count > 0:
            return f"{user}\n[消息含 {image_count} 张图片，审核模型不支持识图，图片未审核]"
        return user

    async def _ask(
        self,
        chat_id: str,
        fallback_chat_id: str,
        system: str,
        user: str,
        image_urls: list = None,
        ocr_chat_id: str = "",
        need_severity: bool = False,
    ) -> Optional[dict]:
        """主模型审核；技术性失败自动切备用。审核模型不识图时由识图模型直接带图出判定。"""
        image_urls = list(image_urls or [])
        ocr = (ocr_chat_id or "").strip()

        # 主模型阶段：不识图且有图 → 识图审核模型直接带图审核（判定即最终结果）
        if image_urls and not self.model_supports_image(chat_id):
            if ocr and ocr != chat_id:
                result = await self._ask_one(
                    ocr, system, user, image_urls, force_vision=True, need_severity=need_severity
                )
                if result is not None:
                    return result
                if self.last_error_type == "risk_block":
                    return None  # 图片触发风控：上抛，由上层按配置判定
                logger.warning(
                    f"[LLMReviewer] 识图审核模型 {ocr} 失败，降级纯文本审核: {self.last_error}"
                )
            # 未配置/失败：主模型纯文本审核
            return await self._ask_one(
                chat_id, system, self._text_only_user(user, len(image_urls)),
                need_severity=need_severity,
            )

        result = await self._ask_one(chat_id, system, user, image_urls, need_severity=need_severity)
        if result is not None:
            return result

        # 备用模型阶段：仅技术性失败切换；风控拦截与配置缺失（config_missing）不切
        fb = (fallback_chat_id or "").strip()
        if fb and fb != chat_id and self.last_error_type not in ("risk_block", "config_missing"):
            prior = self.last_error
            if image_urls and not self.model_supports_image(fb):
                # 兜底模型也不识图：识图审核模型直接带图审核
                if ocr and ocr != fb:
                    result = await self._ask_one(
                        ocr, system, user, image_urls, force_vision=True, need_severity=need_severity
                    )
                    if result is not None:
                        logger.info(
                            f"[LLMReviewer] 主模型 {chat_id} 失败，兜底 {fb} 不识图，"
                            f"已由识图模型 {ocr} 直接带图审核"
                        )
                        self.last_error = ""
                        self.last_error_type = ""
                        return result
                    if self.last_error_type == "risk_block":
                        return None
                # 识图模型不可用：兜底纯文本审核
                result = await self._ask_one(
                    fb, system, self._text_only_user(user, len(image_urls)),
                    need_severity=need_severity,
                )
            else:
                result = await self._ask_one(fb, system, user, image_urls, need_severity=need_severity)
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
        need_severity: bool = False,
    ) -> Optional[dict]:
        """判定一条群消息（可含图片）是否违规。违规时 allowed 为 false。

        单条审核：直接复用批量审核接口，仅消息条数为 1。
        """
        return await self.judge_messages(
            sender,
            [text],
            prompt=prompt,
            chat_id=chat_id,
            fallback_chat_id=fallback_chat_id,
            image_urls=image_urls,
            ocr_chat_id=ocr_chat_id,
            need_severity=need_severity,
        )

    @staticmethod
    def _format_batch(sender: str, texts: list) -> str:
        """把一批（合并审核的）消息格式化为送审正文，保留序号与到达顺序。

        单条消息沿用原有格式（最多送审 _MAX_MSG_CHARS 字）；多条消息逐条编号，
        整批正文超过 _MAX_BATCH_CHARS 时截断，并在尾部说明"内容不完整"，
        避免模型把"没看到"当成"没违规"。
        """
        items = [str(t or "").strip()[:_MAX_MSG_CHARS] for t in (texts or [])]
        if not items:
            items = [""]
        if len(items) == 1:
            body = items[0] if items[0] else "[纯图片消息]"
            return f"发言者：{sender}\n消息内容：{body}"
        lines = []
        for index, text in enumerate(items, 1):
            lines.append(f"{index}. {text if text else '[图片消息]'}")
        body = "\n".join(lines)
        if len(body) > _MAX_BATCH_CHARS:
            body = body[:_MAX_BATCH_CHARS] + "\n…（内容过长已截断，后续消息未完整展示）"
        return (
            f"发言者：{sender}\n"
            f"该成员在短时间内连续发送了 {len(items)} 条消息（按发送顺序编号）：\n{body}\n"
            "请把这些消息作为整体上下文一起判断：其中任意一条违规即整体判为违规，"
            "判定违规时该区间内的这些消息会被一并撤回。"
        )

    async def judge_messages(
        self,
        sender: str,
        texts: list,
        prompt: str = "",
        chat_id: str = "",
        fallback_chat_id: str = "",
        image_urls: list = None,
        ocr_chat_id: str = "",
        need_severity: bool = False,
    ) -> Optional[dict]:
        """判定一批（同一成员短时间内连续发送的）群消息是否违规。

        prompt 为该群自定义审核要求（guard_prompt，由调用方传入），完全由用户定义、
        无内置默认话术；未填写时直接跳过审核（不使用任何内置判定标准）。
        chat_id 为该群选用的 AstrBot 聊天模型，fallback_chat_id 为备用模型；
        image_urls 为这批消息中的图片（与文本一起送审，识图模型直接看图判定），
        ocr_chat_id 为识图审核模型（审核模型不识图时由它直接带图出判定）。
        need_severity 为 True 时（该群开启了「按严重程度决定处置」）额外询问严重度分数。
        当模型输出触发风控特征时，返回带 source="risk_block" 的疑似违规判定；
        其余失败返回 None。
        """
        wanted = str(prompt or "").strip()
        # 不内置任何默认审核标准：未填写审核要求时直接跳过，避免用模型自带标准判定
        if not wanted:
            self.last_error = "未填写审核要求（guard_prompt / 高召回审核要求），已跳过 LLM 审核"
            self.last_error_type = "config_missing"
            logger.warning(f"[LLMReviewer] {self.last_error}")
            return None
        system = f"{wanted}\n{_JSON_RULE}"
        user = self._format_batch(sender, texts)
        image_urls = list(image_urls or [])
        if image_urls and self._vision_available(chat_id, ocr_chat_id):
            # 图片随文本一起送审，需说明图片与编号消息的对应关系，避免模型误判上下文
            shown = min(len(image_urls), _MAX_IMAGES)
            note = f"[本批消息附带了 {shown} 张图片，按发送时间先后排列，请一并审核图片内容]"
            if len(image_urls) > shown:
                note += f"（另有 {len(image_urls) - shown} 张图片超出单次送审上限未附上）"
            user = f"{user}\n{note}"
        result = await self._ask(
            chat_id, fallback_chat_id, system, user,
            image_urls=image_urls, ocr_chat_id=ocr_chat_id, need_severity=need_severity,
        )
        if result is None and self.last_error_type == "risk_block":
            return {
                "allowed": False,
                "reason": "消息触发内容风控，疑似违规",
                "source": "risk_block",
            }
        return result

    def _vision_available(self, chat_id: str, ocr_chat_id: str) -> bool:
        """本次送审是否真的会带图（与 _ask/_ask_one 的带图条件保持一致）。"""
        if not _MULTIMODAL_OK:
            return False
        if self.model_supports_image(chat_id):
            return True
        ocr = (ocr_chat_id or "").strip()
        return bool(ocr and ocr != chat_id)

    async def judge_join_request(
        self,
        comment: str,
        prompt: str = "",
        chat_id: str = "",
        fallback_chat_id: str = "",
        ocr_chat_id: str = "",
    ) -> Optional[dict]:
        """判定入群申请信息是否满足入群要求，返回结构化结果。

        prompt 为该群自定义入群审核要求（join_prompt，由调用方传入），完全由用户定义、
        无内置默认要求；留空时直接跳过自动审批（交由管理员手动处理）。
        chat_id/fallback_chat_id 为入群审批专用模型（未配置时调用方会回退到消息审核模型），
        ocr_chat_id 为识图模型（申请信息含图片时可带图审核）。
        返回字段：allowed（是否满足要求）、has_nickname、has_oid、nickname、oid、reason
        （不通过原因，供拒绝说明使用）、comment（补充说明）。
        失败返回 None（如未配置模型或调用失败），由调用方保守处理。
        """
        if not comment.strip():
            return {
                "allowed": False,
                "has_nickname": False,
                "has_oid": False,
                "nickname": "",
                "oid": "",
                "reason": "入群申请信息为空",
                "comment": "申请信息为空",
            }
        # D1 决策模型不能提取昵称/OID 等字段，入群审批不支持：自动改用备用聊天模型
        if is_d1_model(chat_id):
            fb = str(fallback_chat_id or "").strip()
            if fb and not is_d1_model(fb):
                logger.warning(f"[LLMReviewer] 入群审批不支持 D1 决策模型，已改用备用模型 {fb}")
                chat_id, fallback_chat_id = fb, ""
            else:
                self.last_error = "入群审批不支持 D1 决策模型（需提取昵称/OID），请改选聊天模型"
                self.last_error_type = "request_fail"
                return None
        wanted = str(prompt or "").strip()
        # 不内置任何默认入群审批标准：未填写要求时跳过自动审批，交由管理员手动处理
        if not wanted:
            self.last_error = "未填写入群审批要求（join_prompt），已跳过自动审批"
            self.last_error_type = "config_missing"
            logger.warning(f"[LLMReviewer] {self.last_error}")
            return None
        system = f"{wanted}\n{_JOIN_JSON_RULE}"
        user = f"入群验证信息内容：\n{comment[:500]}"
        result = await self._ask(chat_id, fallback_chat_id, system, user, ocr_chat_id=ocr_chat_id)
        if result is None:
            return None
        raw_allowed = result.get("allowed")
        if raw_allowed is None:
            # 模型未给出结论：不代判、不回退，交由管理员手动处理
            self.last_error = "入群审核模型未返回 allowed 结论，已跳过自动审批"
            self.last_error_type = "parse_fail"
            return None
        allowed = bool(raw_allowed)
        has_nickname = bool(result.get("has_nickname"))
        has_oid = bool(result.get("has_oid"))
        oid = str(result.get("oid") or "").strip()
        reason = str(result.get("reason") or "").strip()
        if not allowed and not reason:
            # 仅拒绝文案占位（避免拒绝消息为空），判定仍以模型结论为准
            reason = "申请信息不满足入群要求"
        return {
            "allowed": allowed,
            "has_nickname": has_nickname,
            "has_oid": has_oid,
            "nickname": str(result.get("nickname") or "").strip(),
            "oid": oid,
            "reason": reason,
            "comment": str(result.get("comment") or "").strip(),
        }