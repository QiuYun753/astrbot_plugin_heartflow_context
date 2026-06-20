"""
心流主动发言插件（Context Plus 依赖版）

必须与 astrbot_plugin_context_plus 捆绑使用。

核心优化：
- 利用 Prefix Caching 共享主 LLM 的缓存
- 读取完整的聊天日志（历史摘要、群成员画像、历史日志）
- 判断准确性大幅提升（有完整历史上下文）
- Token 消耗极低（每次判断只变化约 50 tokens）
- 缓存命中率可达 95-97%

依赖：
- 必须安装并启用 astrbot_plugin_context_plus 插件
- 必须使用支持 Prefix Caching 的模型
- 自动读取 context_plus 的配置参数（无需手动配置）

基于 astrbot_plugin_Heartflow 重构
"""
import json
import re
import time
import datetime
import os
from collections import deque
from typing import Dict
from dataclasses import dataclass
import aiofiles
import aiofiles.os as aio_os

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.api.message_components import Plain


@dataclass
class JudgeResult:
    """判断结果数据类"""
    relevance: float = 0.0
    willingness: float = 0.0
    social: float = 0.0
    timing: float = 0.0
    continuity: float = 0.0
    reasoning: str = ""
    should_reply: bool = False
    confidence: float = 0.0
    overall_score: float = 0.0


@dataclass
class RawMessage:
    """原始群聊消息条目"""
    sender_name: str
    sender_id: str
    content: str
    timestamp: float
    is_bot: bool = False


@dataclass
class ChatState:
    """群聊状态数据类
    
    活跃度系统设计：
    - activity: 活跃度（0.0-1.0），基于消息数量和时间恢复
    - 活跃度高 → 群聊活跃 → 机器人回复意愿高
    - 活跃度低 → 群聊不活跃 → 机器人回复意愿低
    - 活跃度随时间恢复，避免不活跃群聊永远不回复
    
    注意：此数据类中的统计数据不会持久化，重启后会丢失。
    """
    activity: float = 1.0  # 活跃度（替代原来的 energy）
    last_reply_time: float = 0.0
    last_activity_update_time: float = 0.0  # 上次活跃度更新的时间基准
    recent_message_count: int = 0  # 最近消息计数（用于计算活跃度）
    last_reset_date: str = ""
    total_messages: int = 0
    total_replies: int = 0


def _extract_json(text: str) -> dict:
    """从模型返回的文本中稳健地提取 JSON 对象。"""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError(f"无法提取 JSON: {text[:200]}")


def _clamp_score(v) -> float:
    """将分数钉位到 [0, 10]。"""
    try:
        return max(0.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


class HeartflowContextPlugin(star.Star):

    def __init__(self, context: star.Context, config):
        super().__init__(context)
        self.config = config

        # 判断模型配置（必须和主模型相同，才能共享缓存）
        self.judge_provider_name = self.config.get("judge_provider_name", "")

        # 心流参数
        self.reply_threshold = self.config.get("reply_threshold", 0.6)
        self.activity_target_messages = self.config.get("activity_target_messages", 10)
        self.activity_decay_rate = self.config.get("activity_decay_rate", 0.1)
        self.activity_recovery_rate = self.config.get("activity_recovery_rate", 0.02)
        self.activity_min_threshold = self.config.get("activity_min_threshold", 0.3)
        self.min_reply_interval = self.config.get("min_reply_interval_seconds", 0)
        self.whitelist_enabled = self.config.get("whitelist_enabled", False)
        self.chat_whitelist = self.config.get("chat_whitelist", [])

        # 群聊状态管理
        self.chat_states: Dict[str, ChatState] = {}

        # 原始消息缓冲区（预留功能，当前未使用）
        # TODO: 未来可用于实现基于消息历史的更精细判断
        self._raw_msg_buffer: Dict[str, deque] = {}
        self._raw_msg_buffer_size = 20

        # 判断权重
        self.weights = {
            "relevance": self.config.get("judge_relevance", 0.25),
            "willingness": self.config.get("judge_willingness", 0.2),
            "social": self.config.get("judge_social", 0.2),
            "timing": self.config.get("judge_timing", 0.15),
            "continuity": self.config.get("judge_continuity", 0.2),
        }
        weight_sum = sum(self.weights.values())
        if abs(weight_sum - 1.0) > 1e-6:
            # 归一化权重，确保总和为 1（评分计算的正确做法）
            self.weights = {k: v / weight_sum for k, v in self.weights.items()}
            logger.info(
                f"[HeartflowContext] 权重已归一化（原总和={weight_sum:.2f}）| "
                f"relevance={self.weights['relevance']:.2f} | "
                f"willingness={self.weights['willingness']:.2f} | "
                f"social={self.weights['social']:.2f} | "
                f"timing={self.weights['timing']:.2f} | "
                f"continuity={self.weights['continuity']:.2f}"
            )

        self.judge_include_reasoning = self.config.get("judge_include_reasoning", False)
        self.debug_thinking_mode = self.config.get("debug_thinking_mode", False)
        self.log_full_request = self.config.get("log_full_request", False)

        # 聊天日志配置：自动读取 context_plus 的配置
        self.chat_log_max_chars, self.chat_log_days = self._load_context_plus_config()
        
        # 检查提供商配置
        self._check_provider_config()
        
        logger.info("[HeartflowContext] 心流插件（Context Plus 依赖版）已初始化")

    def _load_context_plus_config(self) -> tuple:
        """自动读取 context_plus 的配置参数
        
        返回：(chat_log_max_chars, chat_log_days)
        
        注意：此方法在 __init__ 中同步调用，使用同步 IO 是合理的，
        因为这是插件初始化时的一次性操作，不会阻塞运行时的异步消息处理。
        
        读取策略：
        1. 尝试读取 context_plus 的 config.json
        2. 如果读取成功且包含参数，使用 context_plus 的配置
        3. 如果读取失败或缺少参数，使用 context_plus 的默认值（16000, 5）
        """
        # 构建 context_plus 的配置文件路径
        astrbot_root = os.path.dirname(os.path.dirname(os.path.dirname(StarTools.get_data_dir())))
        context_plus_config_path = os.path.join(
            astrbot_root,
            "data",
            "plugins",
            "astrbot_plugin_context_plus",
            "config.json"
        )
        
        try:
            # 尝试读取 context_plus 的配置文件
            if os.path.exists(context_plus_config_path):
                with open(context_plus_config_path, "r", encoding="utf-8") as f:
                    context_plus_config = json.load(f)
                
                # 读取配置参数
                chat_log_max_chars = context_plus_config.get("chat_log_max_chars", 16000)
                chat_log_days = context_plus_config.get("chat_log_days", 5)
                
                logger.info(
                    f"[HeartflowContext] ✅ 已读取 context_plus 配置 | "
                    f"chat_log_max_chars={chat_log_max_chars} | chat_log_days={chat_log_days}"
                )
                return chat_log_max_chars, chat_log_days
            else:
                logger.warning(
                    "[HeartflowContext] ⚠️ context_plus 配置文件不存在，使用默认配置 | "
                    f"chat_log_max_chars=16000 | chat_log_days=5"
                )
                return 16000, 5
        except Exception as e:
            logger.warning(
                f"[HeartflowContext] ⚠️ 读取 context_plus 配置失败: {e}，使用默认配置 | "
                f"chat_log_max_chars=16000 | chat_log_days=5"
            )
            return 16000, 5

    def _check_provider_config(self):
        """检查提供商配置是否正确
        
        输出所有可用的提供商 ID，帮助用户确认配置。
        """
        # 获取所有可用的提供商
        all_providers = self.context.get_all_providers()
        provider_ids = [p.meta().id for p in all_providers]
        
        logger.info(
            f"[HeartflowContext] 📋 可用的提供商 ID: {provider_ids}"
        )
        
        # 检查配置的提供商是否存在
        if not self.judge_provider_name:
            logger.warning(
                "[HeartflowContext] ⚠️ 未配置 judge_provider_name，请设置提供商 ID"
            )
            return
        
        # 检查提供商是否存在
        judge_provider = self.context.get_provider_by_id(self.judge_provider_name)
        if not judge_provider:
            logger.error(
                f"[HeartflowContext] ❌ 提供商 '{self.judge_provider_name}' 不存在！"
            )
            logger.error(
                f"[HeartflowContext] 💡 请使用以下可用的提供商 ID 之一: {provider_ids}"
            )
            logger.error(
                "[HeartflowContext] 💡 建议: 使用和主模型相同的提供商 ID（如 DeepSeek）以共享缓存"
            )
        else:
            logger.info(
                f"[HeartflowContext] ✅ 提供商 '{self.judge_provider_name}' 已找到，类型: {judge_provider.__class__.__name__}"
            )

    # =================================================================
    # 主入口
    # =================================================================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊消息处理入口。"""
        if not self._should_process_message(event):
            return
        self._record_raw_message(event, is_bot=False)

        try:
            judge_result = await self._judge(event)
            if judge_result.should_reply:
                logger.info(
                    f"[HeartflowContext] 🔥 心流触发回复 | {event.unified_msg_origin[:20]}... "
                    f"评分:{judge_result.overall_score:.2f}"
                )
                event.is_at_or_wake_command = True
                event.set_extra("heartflow_triggered", True)
                self._update_active_state(event)
            else:
                self._update_passive_state(event)
        except Exception as e:
            logger.error(f"[HeartflowContext] 心流判断异常: {e}")
            # 异常时也需要更新被动状态，确保活跃度恢复逻辑正常执行
            self._update_passive_state(event)

    # =================================================================
    # 缓存优化的 LLM judge
    # =================================================================

    def _get_chat_log_dir(self, event: AstrMessageEvent) -> str:
        """获取 context_plus 插件的聊天日志目录。
        
        注意：本插件必须与 astrbot_plugin_context_plus 捆绑使用，
        需要读取 context_plus 的日志目录以共享 DeepSeek Prefix Caching。
        
        路径结构：
        - context_plus: {StarTools.get_data_dir()}/chat_logs/{group_id}
        - 本插件读取相同路径，确保前缀一致
        """
        # context_plus 的数据目录结构：{data_dir}/chat_logs/{group_id}
        # StarTools.get_data_dir() 返回的是 AstrBot/data/plugin_data/{plugin_name}/
        # 我们需要访问 astrbot_plugin_context_plus 的数据目录
        
        # 方法：从当前插件的数据目录向上追溯到 AstrBot 根目录，然后定位到 context_plus
        # 当前插件数据目录：AstrBot/data/plugin_data/astrbot_plugin_heartflow_context/
        # context_plus 数据目录：AstrBot/data/plugin_data/astrbot_plugin_context_plus/
        
        # 更简单的方法：直接使用 StarTools.get_data_dir() 的父目录
        plugin_data_dir = os.path.dirname(StarTools.get_data_dir())
        context_plus_data_dir = os.path.join(plugin_data_dir, "astrbot_plugin_context_plus")
        
        group_id = event.get_group_id()
        if not group_id:
            return ""
        
        return os.path.join(context_plus_data_dir, "chat_logs", group_id)

    async def _read_chat_logs_for_judge(self, event: AstrMessageEvent) -> str:
        """读取记忆库缓存中的聊天日志（与主 LLM 看到的格式一致）。

        读取策略（与 context_plus 一致，确保共享 DeepSeek 缓存）：
        - 每日摘要 → 始终加载（不计入预算，最前面）
        - 全局画像 → 始终加载（紧随摘要之后）
        - 当天日志 → 始终加载完整（不计入预算，放在末尾）
        - 昨天→前天→... → 从昨天往前，累计不超过 chats_max_chars，
          一旦超限则跳过该天及之前所有天
        """
        log_dir = self._get_chat_log_dir(event)
        if not log_dir or not await aio_os.path.exists(log_dir):
            return ""
        parts = []

        # 1. 摘要文件（始终加载，最前面，与 context_plus 顺序一致）
        summary_path = os.path.join(log_dir, "_summary.log")
        if await aio_os.path.exists(summary_path):
            try:
                async with aiofiles.open(summary_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                if content.strip():
                    parts.append(f"<historical_summary>\n{content.strip()}\n</historical_summary>")
            except Exception:
                pass

        # 2. 全局群成员画像（紧随摘要之后，与 context_plus 顺序一致）
        profile_path = os.path.join(log_dir, "_profile.md")
        if await aio_os.path.exists(profile_path):
            try:
                async with aiofiles.open(profile_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                if content.strip():
                    parts.append(f"<group_profile>\n{content.strip()}\n</group_profile>")
            except Exception:
                pass

        # 使用配置值（必须与 context_plus 一致以共享缓存）
        chats_max_chars = self.chat_log_max_chars
        max_days = self.chat_log_days

        # 3. 当天日志必须全部加载（不计入预算，放在末尾）
        today = datetime.datetime.now()
        today_str = today.strftime("%Y-%m-%d")
        today_path = os.path.join(log_dir, f"{today_str}.log")
        today_formatted = ""
        if await aio_os.path.exists(today_path):
            try:
                async with aiofiles.open(today_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                if content.strip():
                    today_formatted = f"<chat_logs_{today_str}>\n{content.strip()}\n</chat_logs_{today_str}>"
            except Exception:
                pass

        # 4. 从昨天开始往前遍历，累计预算内尽可能加载历史日志
        daily_logs = []
        accumulated_size = 0

        for day_offset in range(1, max_days):  # 1=昨天, 2=前天, ...
            date_str = (today - datetime.timedelta(days=day_offset)).strftime("%Y-%m-%d")
            log_path = os.path.join(log_dir, f"{date_str}.log")
            if not await aio_os.path.exists(log_path):
                continue
            try:
                async with aiofiles.open(log_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                if not content.strip():
                    continue
                formatted = f"<chat_logs_{date_str}>\n{content.strip()}\n</chat_logs_{date_str}>"
                if accumulated_size + len(formatted) > chats_max_chars:
                    break
                daily_logs.append(formatted)
                accumulated_size += len(formatted)
            except Exception:
                pass

        # 5. 拼装：摘要 → 画像 → 历史日志（从旧到新）→ 当天日志
        summary_part = "\n\n".join(parts) if parts else ""
        history_part = "\n\n".join(daily_logs) if daily_logs else ""
        result_parts = [p for p in [summary_part, history_part, today_formatted] if p]
        if not result_parts:
            return ""
        return "\n\n".join(result_parts)

    async def _judge(self, event: AstrMessageEvent) -> JudgeResult:
        """用 LLM 进行语义判断，利用记忆库缓存。

        缓存策略：
        - system_prompt = 人设 + 聊天日志（与主 LLM 相同内容，已缓存）
        - prompt = 固定评分指令 + 当前消息（固定指令部分也会缓存）
        - 每次判断只变化约 50 tokens（当前消息）
        """
        if not self.judge_provider_name:
            return JudgeResult(should_reply=False, reasoning="未配置提供商")

        judge_provider = self.context.get_provider_by_id(self.judge_provider_name)
        if not judge_provider:
            return JudgeResult(should_reply=False, reasoning="提供商不存在")

        # 构建 system_prompt：人设 + 聊天日志（已缓存）+ 固定评分指令（也进入缓存）
        persona_prompt = await self._get_persona_system_prompt(event)
        chat_logs = await self._read_chat_logs_for_judge(event)
        
        # 拼接 system_prompt：确保前缀与 context_plus 一致
        # context_plus 格式：人设内容（末尾无换行符）+ "\n\n" + 聊天日志
        # _get_persona_system_prompt 已经去掉了末尾的换行符
        system_prompt = persona_prompt if persona_prompt else ""
        if chat_logs:
            system_prompt += f"\n\n{chat_logs}"  # 加上两个换行符（与 context_plus 一致）

        # 固定评分指令放在 system_prompt 末尾（位于缓存前缀内，不会被当日志变化打断）
        has_reasoning = self.judge_include_reasoning
        reasoning_output = ',\n    "reasoning": "详细分析原因，说明为什么应该或不应该回复，结合聊天记录进行分析"' if has_reasoning else ""
        judge_instruction = (
            "\n\n### 回复判断指令\n"
            "你是一个群聊回复决策系统。请判断是否应该主动回复以下消息。\n\n"
            "### 评分维度（0-10 整数）\n"
            "- relevance：消息是否有趣、有回复价值\n"
            "- willingness：基于当前活跃度，回复意愿\n"
            "- social：当前氛围下回复是否合适\n"
            "- timing：回复时机是否恰当\n"
            "- continuity：与当前群聊话题的相关度\n\n"
            f"### 回复阈值\n({self.reply_threshold} 分以上才回复)\n\n"
            "### 输出格式（只返回 JSON，不要其他内容）\n"
            f'{{"relevance":7,"willingness":6,"social":8,"timing":7,"continuity":6{reasoning_output}}}\n'
        )
        system_prompt += judge_instruction

        chat_state = self._get_chat_state(event.unified_msg_origin)

        # 当前消息（唯一不缓存的部分）
        user_content = (
            f"活跃度:{chat_state.activity:.1f} | "
            f"{event.get_sender_name()}: {event.message_str}"
        )

        try:
            llm_response = await judge_provider.text_chat(
                prompt=user_content,
                system_prompt=system_prompt if system_prompt else None,
                contexts=[],
                image_urls=[],
            )
            
            # 记录缓存命中率（如果有 usage 信息）
            cache_hit_rate = 0.0
            if hasattr(llm_response, 'usage') and llm_response.usage:
                usage = llm_response.usage
                # 使用 context_plus 的字段名
                input_other = getattr(usage, 'input_other', 0) or 0
                input_cached = getattr(usage, 'input_cached', 0) or 0
                
                total_input = input_other + input_cached
                if total_input > 0:
                    cache_hit_rate = (input_cached / total_input) * 100
                    logger.info(
                        f"[HeartflowContext] 💾 缓存命中率: {cache_hit_rate:.1f}% | "
                        f"cached={input_cached} | other={input_other} | total={total_input}"
                    )
            
            # 记录完整请求（如果开启）
            if self.log_full_request:
                await self._log_llm_request_debug(
                    event, system_prompt, user_content, llm_response.completion_text,
                    cache_hit_rate
                )
            
            # DEBUG: 检查思考模式是否关闭（reasoning_content 应为空）
            if self.debug_thinking_mode:
                if llm_response.reasoning_content:
                    logger.warning(f"[HeartflowContext] ⚠️ 思考模式未关闭！reasoning_content 长度: {len(llm_response.reasoning_content)}")
                else:
                    logger.info("[HeartflowContext] ✅ 思考模式已关闭，无 reasoning_content")
            content = llm_response.completion_text.strip()
            judge_data = _extract_json(content)

            relevance = _clamp_score(judge_data.get("relevance", 0))
            willingness = _clamp_score(judge_data.get("willingness", 0))
            social = _clamp_score(judge_data.get("social", 0))
            timing = _clamp_score(judge_data.get("timing", 0))
            continuity = _clamp_score(judge_data.get("continuity", 0))
            reasoning = judge_data.get("reasoning", "") if has_reasoning else ""

            overall = (
                relevance * self.weights["relevance"]
                + willingness * self.weights["willingness"]
                + social * self.weights["social"]
                + timing * self.weights["timing"]
                + continuity * self.weights["continuity"]
            ) / 10.0

            should = overall >= self.reply_threshold
            reason_str = f" | 理由:{reasoning[:80]}" if has_reasoning else ""
            logger.info(
                f"[HeartflowContext] 📊 心流评分 | 消息:{event.message_str[:40]} | "
                f"总分:{overall:.3f}{'🔥' if should else '❌'} | "
                f"r={relevance} w={willingness} s={social} t={timing} c={continuity}{reason_str}"
            )
            return JudgeResult(
                relevance=relevance, willingness=willingness,
                social=social, timing=timing, continuity=continuity,
                reasoning=reasoning, should_reply=should,
                confidence=overall, overall_score=overall,
            )
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[HeartflowContext] 心流判断 JSON 解析失败: {e}")
            return JudgeResult(should_reply=False, reasoning=f"解析失败: {e}")
        except Exception as e:
            logger.warning(f"[HeartflowContext] 心流判断调用失败: {e}")
            return JudgeResult(should_reply=False, reasoning=f"异常: {e}")

    # =================================================================
    # LLM 请求处理
    # =================================================================

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """心流触发时，注入主动发言提示。"""
        if not event.get_extra("heartflow_triggered"):
            return
        if not req or not hasattr(req, "system_prompt"):
            return
        note = (
            "\n（注意：本次是你主动参与群聊，并非被用户点名。"
            "请基于聊天日志中的群聊氛围，自然地加入当前话题。"
            "回复应简短自然，像普通群成员一样。）"
        )
        req.system_prompt = (req.system_prompt or "") + note

    # =================================================================
    # 消息发送后处理
    # =================================================================

    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent):
        """记录机器人回复到缓冲区。"""
        if not self.config.get("enable_heartflow", False):
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        reply_text = "".join(
            comp.text for comp in result.chain if isinstance(comp, Plain)
        ).strip()
        if not reply_text:
            return
        umo = event.unified_msg_origin
        if umo not in self._raw_msg_buffer:
            self._raw_msg_buffer[umo] = deque(maxlen=self._raw_msg_buffer_size)
        self._raw_msg_buffer[umo].append(RawMessage(
            sender_name="bot", sender_id="bot",
            content=reply_text, timestamp=time.time(), is_bot=True,
        ))

    # =================================================================
    # 状态管理
    # =================================================================

    def _should_process_message(self, event: AstrMessageEvent) -> bool:
        """检查是否应该处理这条消息。"""
        if not self.config.get("enable_heartflow", False):
            return False
        if event.is_at_or_wake_command:
            return False
        if self.whitelist_enabled:
            if not self.chat_whitelist:
                return False
            if event.unified_msg_origin not in self.chat_whitelist:
                return False
        if event.get_sender_id() == event.get_self_id():
            return False
        if not event.message_str or not event.message_str.strip():
            return False
        if self.min_reply_interval > 0:
            minutes = self._get_minutes_since_last_reply(event.unified_msg_origin)
            if minutes * 60 < self.min_reply_interval:
                return False
        return True

    def _get_chat_state(self, chat_id: str) -> ChatState:
        """获取群聊状态。"""
        if chat_id not in self.chat_states:
            self.chat_states[chat_id] = ChatState()
        today = datetime.date.today().isoformat()
        state = self.chat_states[chat_id]
        if state.last_reset_date != today:
            state.last_reset_date = today
            # 每日重置活跃度（给予一定的初始活跃度）
            state.activity = max(state.activity, 0.5)
        # 注意：这里不应该更新 last_reply_time
        # 活跃度恢复应该在 _update_passive_state 中处理
        # 否则会导致 _get_minutes_since_last_reply 每次调用都重置时间
        return state

    def _get_minutes_since_last_reply(self, chat_id: str) -> int:
        """获取距离上次回复的分钟数。
        
        返回值：
        - 如果从未回复过（last_reply_time == 0），返回一个很大的值（999 分钟）
          这表示"很久以前"，时间间隔检查会通过
        - 否则返回实际的分钟数
        """
        state = self._get_chat_state(chat_id)
        if state.last_reply_time == 0:
            return 999  # 表示从未回复过，时间间隔检查会通过
        return int((time.time() - state.last_reply_time) / 60)

    def _update_active_state(self, event: AstrMessageEvent):
        """回复后的状态更新。"""
        state = self._get_chat_state(event.unified_msg_origin)
        current_time = time.time()
        state.last_reply_time = current_time
        state.last_activity_update_time = current_time  # 同步更新活跃度时间基准
        state.total_replies += 1
        state.total_messages += 1
        # 活跃度下降（回复消耗活跃度）
        state.activity = max(self.activity_min_threshold, state.activity - self.activity_decay_rate)

    def _update_passive_state(self, event: AstrMessageEvent):
        """不回复时的状态更新。"""
        state = self._get_chat_state(event.unified_msg_origin)
        state.total_messages += 1
        state.recent_message_count += 1
        
        # 活跃度恢复：基于消息数量和时间流逝
        current_time = time.time()
        
        # 1. 基于消息数量增加活跃度（群聊越活跃，机器人越活跃）
        message_activity = min(1.0, state.recent_message_count / self.activity_target_messages)
        
        # 2. 基于时间流逝恢复活跃度（避免不活跃群聊永远不回复）
        time_recovery = 0.0
        if state.last_activity_update_time > 0:
            elapsed_minutes = (current_time - state.last_activity_update_time) / 60.0
            if elapsed_minutes > 0:
                time_recovery = elapsed_minutes * self.activity_recovery_rate
        else:
            # 首次初始化，使用 last_reply_time 作为基准（如果存在）
            if state.last_reply_time > 0:
                elapsed_minutes = (current_time - state.last_reply_time) / 60.0
                if elapsed_minutes > 0:
                    time_recovery = elapsed_minutes * self.activity_recovery_rate
        
        # 3. 综合计算活跃度：消息活跃度 + 时间恢复，但不超过 1.0
        state.activity = min(1.0, max(self.activity_min_threshold, 
                                      message_activity + time_recovery))
        
        # 更新活跃度更新的时间基准
        state.last_activity_update_time = current_time

    def _record_raw_message(self, event: AstrMessageEvent, is_bot: bool):
        """记录原始消息到缓冲区。"""
        umo = event.unified_msg_origin
        if umo not in self._raw_msg_buffer:
            self._raw_msg_buffer[umo] = deque(maxlen=self._raw_msg_buffer_size)
        self._raw_msg_buffer[umo].append(RawMessage(
            sender_name=event.get_sender_name(),
            sender_id=event.get_sender_id(),
            content=event.message_str.strip(),
            timestamp=time.time(),
            is_bot=is_bot,
        ))

    async def _get_persona_system_prompt(self, event: AstrMessageEvent) -> str:
        """获取当前对话的人格 system_prompt。
        
        从 context_plus 保存的人设文件读取，确保前缀一致。
        文件位置：{context_plus_data_dir}/personas/{group_id}.txt
        """
        try:
            # 从 context_plus 的数据目录读取人设
            plugin_data_dir = os.path.dirname(StarTools.get_data_dir())
            context_plus_data_dir = os.path.join(plugin_data_dir, "astrbot_plugin_context_plus")
            
            group_id = event.get_group_id()
            if not group_id:
                logger.warning("[HeartflowContext] 无法获取 group_id，返回空字符串")
                return ""
            
            persona_file = os.path.join(context_plus_data_dir, "personas", f"{group_id}.txt")
            
            if os.path.exists(persona_file):
                async with aiofiles.open(persona_file, "r", encoding="utf-8") as f:
                    persona_prompt = await f.read()
                    if persona_prompt:
                        # 去掉末尾的换行符，确保与 context_plus 保存的格式一致
                        persona_prompt = persona_prompt.rstrip()
                        logger.debug(
                            f"[HeartflowContext] 从 context_plus 读取人设成功，长度: {len(persona_prompt)} 字符"
                        )
                        return persona_prompt
                    else:
                        logger.warning(f"[HeartflowContext] 人设文件为空: {persona_file}")
            else:
                logger.warning(
                    f"[HeartflowContext] 人设文件不存在: {persona_file}。"
                    "请确保 context_plus 正常运行并已保存人设。"
                )
            
            return ""
        except Exception as e:
            logger.warning(f"[HeartflowContext] 读取人设失败: {e}")
            return ""

    async def _log_llm_request_debug(
        self, event: AstrMessageEvent, system_prompt: str,
        user_content: str, response: str, cache_hit_rate: float
    ) -> None:
        """将 LLM 请求的完整信息写入调试日志文件（开关控制）。
        
        调试日志文件位置: {data_dir}/debug_llm_requests/{group_id}.log
        每次写入包含时间戳、system_prompt、prompt、response、缓存命中率等关键信息。
        
        参考 astrbot_plugin_context_plus 的实现，确保格式一致。
        """
        try:
            # 获取数据目录
            data_dir = StarTools.get_data_dir()
            debug_dir = os.path.join(data_dir, "debug_llm_requests")
            os.makedirs(debug_dir, exist_ok=True)
            
            # 使用 unified_msg_origin 作为文件名
            # 移除 Windows 文件名不允许的字符：: < > " / \ | ? *
            group_id = event.unified_msg_origin
            for char in ['<', '>', ':', '"', '/', '\\', '|', '?', '*']:
                group_id = group_id.replace(char, '_')
            log_path = os.path.join(debug_dir, f"{group_id}.log")
            
            sender_name = event.get_sender_name() or "未知"
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 构建日志内容（参考 context_plus 的格式）
            separator = "=" * 72
            log_entry = (
                f"{separator}\n"
                f"时间: {timestamp}\n"
                f"群组: {event.unified_msg_origin}\n"
                f"发送者: {sender_name}\n"
                f"场景: 心流判断\n"
                f"原始消息: {event.message_str}\n"
                f"--- system_prompt ---\n"
                f"{system_prompt or '(空)'}\n"
                f"--- prompt ---\n"
                f"{user_content or '(空)'}\n"
                f"--- response ---\n"
                f"{response or '(空)'}\n"
                f"--- 缓存命中率 ---\n"
                f"{cache_hit_rate:.1f}%\n"
                f"{separator}\n\n"
            )
            
            async with aiofiles.open(log_path, "a", encoding="utf-8") as f:
                await f.write(log_entry)
            
            logger.info(f"[HeartflowContext] 调试日志已写入: {log_path}")
        except Exception as e:
            logger.error(f"[HeartflowContext] 写入 LLM 请求调试日志失败: {e}")