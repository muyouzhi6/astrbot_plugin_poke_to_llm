"""
AstrBot 戳一戳触发 LLM 插件

在群聊中戳一戳 Bot，触发 LLM 响应。

场景覆盖：
- 发消息忘了 @Bot，戳一戳提醒 Bot 回应之前的内容
- 想和 Bot 聊天
- 希望 Bot 参与当前话题或回答问题
- 对 Bot 之前说的话有反应
- 单纯戳着玩

设计原则：
- 通过 yield event.request_llm() 走标准 LLM 链路
- 自动兼容 context_aware 插件（通过框架的 on_llm_request 钩子）
- 自动兼容框架自带的对话上下文
- 对话会记入上下文历史
- 轻量高效，可持久运行

Author: 木有知
"""

from __future__ import annotations

import time
from string import Template
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

if TYPE_CHECKING:
    from astrbot.core.config import AstrBotConfig
    from astrbot.core.db.po import Conversation

# 版本号常量
VERSION = "2.2.0"

# 默认提示词（使用 $variable 语法，支持 safe_substitute）
DEFAULT_POKE_PROMPT = """$username 戳了戳你。

可能的情况：
- 刚才说话忘了@你，希望你回应之前的内容
- 想和你聊天
- 希望你参与当前话题或回答问题
- 对你之前说的话有反应
- 只是单纯戳你玩

请根据最近的对话上下文，判断用户意图并自然回应。如果上下文没有明确话题，可以俏皮地回应这个戳一戳。"""

# 冷却清理间隔（秒）
COOLDOWN_EXPIRE_SECONDS = 600
# 每多少次触发一次冷却清理
CLEANUP_INTERVAL = 50


class PokeToLLM(Star):
    """
    戳一戳触发 LLM 插件

    监听戳一戳事件，通过标准 LLM 链路生成回复。
    自动兼容 context_aware 插件和框架自带上下文。
    """

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self._config = config

        # 加载配置
        self._enabled = bool(self._cfg("enable", True))
        self._enable_in_groups = bool(self._cfg("enable_in_groups", True))
        self._enable_in_private = bool(self._cfg("enable_in_private", True))

        # 冷却时间配置，安全转换
        cooldown_val = self._cfg("cooldown", 5.0)
        try:
            self._cooldown = float(cooldown_val) if cooldown_val is not None else 5.0
        except (ValueError, TypeError):
            logger.warning(f"[PokeToLLM] cooldown 配置值无效: {cooldown_val}，使用默认值 5.0")
            self._cooldown = 5.0

        self._poke_prompt = str(self._cfg("poke_prompt", DEFAULT_POKE_PROMPT))

        # 白名单和黑名单（使用 frozenset 提高查找效率，不可变）
        enabled_groups_raw = self._cfg("enabled_groups", [])
        self._enabled_groups: frozenset[str] = frozenset(
            str(g) for g in (enabled_groups_raw if isinstance(enabled_groups_raw, list) else []) if g
        )

        blacklisted_raw = self._cfg("blacklisted_users", [])
        self._blacklisted_users: frozenset[str] = frozenset(
            str(u) for u in (blacklisted_raw if isinstance(blacklisted_raw, list) else []) if u
        )

        # 冷却记录: user_id -> last_poke_time
        self._cooldown_map: dict[str, float] = {}

        # 统计
        self._poke_count = 0

        logger.info(f"[PokeToLLM] 插件 v{VERSION} 已加载")

    def _cfg(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        if self._config is None:
            return default
        return self._config.get(key, default)

    def _check_cooldown(self, user_id: str) -> bool:
        """检查冷却时间，返回 True 表示可以响应"""
        now = time.time()
        last_time = self._cooldown_map.get(user_id, 0.0)
        if now - last_time < self._cooldown:
            return False
        self._cooldown_map[user_id] = now
        return True

    def _cleanup_cooldown(self) -> None:
        """清理过期的冷却记录"""
        now = time.time()
        # 使用列表推导避免在迭代时修改字典
        expired = [uid for uid, ts in self._cooldown_map.items() if now - ts > COOLDOWN_EXPIRE_SECONDS]
        for uid in expired:
            del self._cooldown_map[uid]

    def _is_poke_event(self, raw_message: dict[str, Any]) -> bool:
        """检查是否为戳一戳事件"""
        return (
            raw_message.get("post_type") == "notice"
            and raw_message.get("notice_type") == "notify"
            and raw_message.get("sub_type") == "poke"
        )

    def _format_prompt(self, username: str) -> str:
        """格式化提示词，使用 safe_substitute 容错处理未知占位符"""
        template = Template(self._poke_prompt)
        return template.safe_substitute(username=username)

    def _format_prompt_with_context(self, username: str, group_id: str | None) -> str:
        """当 conversation 获取失败时，生成包含额外上下文的降级提示词"""
        context_type = "群聊" if group_id else "私聊"
        base_prompt = self._format_prompt(username)
        fallback_note = f"\n\n[注意：当前为{context_type}场景，无法获取历史上下文，请基于这条戳一戳事件本身进行回应]"
        return base_prompt + fallback_note

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """监听并响应戳一戳事件"""
        # ===== 早期返回：最便宜的检查放最前 =====

        # 1. 插件未启用（纯内存检查，最快）
        if not self._enabled:
            return

        # 2. 平台检查（字符串比较，很快）
        if event.get_platform_name() != "aiocqhttp":
            return

        # 3. 获取原始消息
        raw_message = getattr(event.message_obj, "raw_message", None)
        if not raw_message or not isinstance(raw_message, dict):
            return

        # 4. 检查是否为戳一戳事件
        if not self._is_poke_event(raw_message):
            return

        # ===== 提取事件信息 =====
        # 戳一戳是 notice 事件，优先使用框架 API，fallback 到 raw_message
        # target_id 是戳一戳特有字段，必须从 raw_message 获取
        bot_id = event.get_self_id() or raw_message.get("self_id")
        sender_id = event.get_sender_id() or raw_message.get("user_id")
        target_id = raw_message.get("target_id")  # 戳一戳特有字段
        group_id = event.get_group_id() or raw_message.get("group_id")

        # 必须是戳机器人
        if not bot_id or not sender_id or not target_id:
            return
        if str(target_id) != str(bot_id):
            return

        sender_id_str = str(sender_id)

        # ===== 权限和作用域检查 =====

        # 黑名单检查
        if sender_id_str in self._blacklisted_users:
            logger.debug(f"[PokeToLLM] 用户 {sender_id} 在黑名单中，忽略")
            return

        # 作用域检查
        if group_id:
            if not self._enable_in_groups:
                return
            if self._enabled_groups and str(group_id) not in self._enabled_groups:
                return
        elif not self._enable_in_private:
            return

        # 冷却检查
        if not self._check_cooldown(sender_id_str):
            logger.debug(f"[PokeToLLM] 用户 {sender_id} 冷却中，忽略")
            return

        # ===== 通过所有检查，处理戳一戳 =====
        self._poke_count += 1

        # 定期清理冷却记录
        if self._poke_count % CLEANUP_INTERVAL == 0:
            self._cleanup_cooldown()

        # 获取用户名
        username = event.get_sender_name() or sender_id_str

        logger.info(
            f"[PokeToLLM] #{self._poke_count} | "
            f"用户: {username}({sender_id}) | "
            f"群: {group_id or '私聊'}"
        )

        # 获取 conversation 用于记录对话历史
        conversation = await self._get_conversation(event)
        if not conversation:
            logger.warning("[PokeToLLM] 无法获取 conversation，将以无上下文模式响应")
            # 降级策略：在 prompt 中补充额外上下文信息
            prompt = self._format_prompt_with_context(username, group_id)
        else:
            prompt = self._format_prompt(username)

        # 设置戳一戳触发标记，让 context_aware 插件识别
        event.set_extra("_poke_trigger", True)
        event.set_extra("_poke_sender_id", sender_id_str)
        event.set_extra("_poke_sender_name", username)

        # 通过标准 LLM 链路请求
        yield event.request_llm(prompt=prompt, conversation=conversation)

    async def _get_conversation(self, event: AstrMessageEvent) -> Conversation | None:
        """获取当前会话的 conversation 对象"""
        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager

        try:
            # 获取或创建对话 ID
            cid = await conv_mgr.get_curr_conversation_id(umo)
            if not cid:
                cid = await conv_mgr.new_conversation(umo, event.get_platform_id())

            # 获取对话对象
            return await conv_mgr.get_conversation(umo, cid)
        except Exception:
            logger.exception("[PokeToLLM] 获取 conversation 失败")
            return None

    async def terminate(self) -> None:
        """清理资源"""
        logger.info(
            f"[PokeToLLM] 插件已终止 | "
            f"共响应 {self._poke_count} 次戳一戳 | "
            f"冷却记录: {len(self._cooldown_map)} 条"
        )
