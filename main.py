"""
AstrBot 戳一戳触发 LLM 插件 v1.2.0

在群聊中戳一戳 Bot，触发 LLM 响应。

场景覆盖：
- 发消息忘了 @Bot，戳一戳提醒 Bot 回应之前的内容
- 想和 Bot 聊天
- 希望 Bot 参与当前话题或回答问题
- 对 Bot 之前说的话有反应
- 单纯戳着玩

设计原则：
- 通过 yield event.request_llm() 走标准 LLM 链路
- 支持 context_aware 插件（获取群聊完整上下文）
- 支持框架自带的对话上下文
- 对话会记入上下文历史
- 轻量高效，可持久运行

Author: 木有知
Version: 1.2.0
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

if TYPE_CHECKING:
    from astrbot.core.config import AstrBotConfig


DEFAULT_POKE_PROMPT = """{username}戳了戳你。

可能的情况：
- 刚才说话忘了@你，希望你回应之前的内容
- 想和你聊天
- 希望你参与当前话题或回答问题
- 对你之前说的话有反应
- 只是单纯戳你玩

请根据最近的对话上下文，判断用户意图并自然回应。如果上下文没有明确话题，可以俏皮地回应这个戳一戳。"""


@register(
    "astrbot_plugin_poke_to_llm",
    "木有知",
    "忘@了戳一下吧 - 戳一戳触发 LLM 回复",
    "1.2.0",
    "https://github.com/muyouzhi6/astrbot_plugin_poke_to_llm",
)
class PokeToLLM(Star):
    """
    戳一戳触发 LLM 插件

    监听戳一戳事件，通过标准 LLM 链路生成回复。
    支持 context_aware 插件和框架自带上下文两种模式。
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
        cooldown_val = self._cfg("cooldown", 5.0)
        self._cooldown = float(cooldown_val) if cooldown_val is not None else 5.0
        self._poke_prompt = str(self._cfg("poke_prompt", DEFAULT_POKE_PROMPT))

        # context_aware 模式
        self._use_context_aware = bool(self._cfg("use_context_aware", False))
        self._context_aware_count = int(self._cfg("context_aware_count", 10) or 10)

        # 白名单和黑名单
        self._enabled_groups: set[str] = set()
        enabled_groups_raw = self._cfg("enabled_groups", [])
        if isinstance(enabled_groups_raw, list):
            self._enabled_groups = {str(g) for g in enabled_groups_raw if g}

        self._blacklisted_users: set[str] = set()
        blacklisted_raw = self._cfg("blacklisted_users", [])
        if isinstance(blacklisted_raw, list):
            self._blacklisted_users = {str(u) for u in blacklisted_raw if u}

        # 冷却记录: user_id -> last_poke_time
        self._cooldown_map: dict[str, float] = {}

        # 统计
        self._poke_count = 0

        # context_aware 插件实例缓存
        self._context_aware_plugin: Any = None
        self._context_aware_checked = False

        mode = "context_aware" if self._use_context_aware else "框架对话历史"
        logger.info(f"[PokeToLLM] 插件 v1.2.0 已加载 | 上下文模式: {mode}")

    def _cfg(self, key: str, default=None):
        """获取配置项"""
        if self._config is None:
            return default
        return self._config.get(key, default)

    def _get_context_aware_plugin(self) -> Any:
        """获取 context_aware 插件实例"""
        if self._context_aware_checked:
            return self._context_aware_plugin

        self._context_aware_checked = True

        # 查找 context_aware 插件
        star_meta = self.context.get_registered_star("astrbot_plugin_context_aware")
        if star_meta and star_meta.star_cls:
            self._context_aware_plugin = star_meta.star_cls
            logger.info("[PokeToLLM] 已找到 context_aware 插件")
        else:
            logger.warning(
                "[PokeToLLM] 未找到 context_aware 插件，将使用框架对话历史"
            )

        return self._context_aware_plugin

    def _check_cooldown(self, user_id: str) -> bool:
        """检查冷却时间，返回 True 表示可以响应"""
        now = time.time()
        last_time = self._cooldown_map.get(user_id, 0)
        if now - last_time < self._cooldown:
            return False
        self._cooldown_map[user_id] = now
        return True

    def _cleanup_cooldown(self) -> None:
        """清理过期的冷却记录（超过 10 分钟）"""
        now = time.time()
        expired = [
            uid for uid, ts in self._cooldown_map.items()
            if now - ts > 600
        ]
        for uid in expired:
            del self._cooldown_map[uid]

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """监听并响应戳一戳事件"""
        # 插件未启用
        if not self._enabled:
            return

        # 仅处理 aiocqhttp 平台
        if event.get_platform_name() != "aiocqhttp":
            return

        # 获取原始消息
        raw_message = getattr(event.message_obj, "raw_message", None)
        if not raw_message:
            return

        # 检查是否为戳一戳事件
        if (
            raw_message.get("post_type") != "notice"
            or raw_message.get("notice_type") != "notify"
            or raw_message.get("sub_type") != "poke"
        ):
            return

        # 提取事件信息
        bot_id = raw_message.get("self_id")
        sender_id = raw_message.get("user_id")
        target_id = raw_message.get("target_id")
        group_id = raw_message.get("group_id")

        # 必须是戳机器人
        if not bot_id or not sender_id or not target_id:
            return
        if str(target_id) != str(bot_id):
            return

        # 黑名单检查
        if str(sender_id) in self._blacklisted_users:
            logger.debug(f"[PokeToLLM] 用户 {sender_id} 在黑名单中，忽略")
            return

        # 作用域检查
        if group_id:
            # 群聊
            if not self._enable_in_groups:
                return
            # 白名单检查（空白名单=全部启用）
            if self._enabled_groups and str(group_id) not in self._enabled_groups:
                return
        else:
            # 私聊
            if not self._enable_in_private:
                return

        # 冷却检查
        if not self._check_cooldown(str(sender_id)):
            logger.debug(f"[PokeToLLM] 用户 {sender_id} 冷却中，忽略")
            return

        self._poke_count += 1

        # 定期清理冷却记录（每 50 次）
        if self._poke_count % 50 == 0:
            self._cleanup_cooldown()

        # 获取用户名
        username = event.get_sender_name() or str(sender_id)

        # 构造提示词
        prompt = self._poke_prompt.format(username=username)

        # 获取对话历史上下文
        context_text = ""
        if self._use_context_aware:
            context_text = self._get_context_aware_context(event)

        # 如果有群聊上下文，添加到提示词
        if context_text:
            prompt = f"{context_text}\n\n{prompt}"

        logger.info(
            f"[PokeToLLM] #{self._poke_count} | "
            f"用户: {username}({sender_id}) | "
            f"群: {group_id or '私聊'} | "
            f"上下文: {'context_aware' if context_text else '框架历史'}"
        )

        # 获取 conversation 用于记录对话历史
        conversation = await self._get_conversation(event)

        # 通过标准 LLM 链路请求
        yield event.request_llm(prompt=prompt, conversation=conversation)

    def _get_context_aware_context(self, event: AstrMessageEvent) -> str:
        """从 context_aware 插件获取群聊上下文"""
        plugin = self._get_context_aware_plugin()
        if not plugin:
            return ""

        try:
            # 调用 context_aware 的公共 API
            if hasattr(plugin, "get_formatted_context"):
                context = plugin.get_formatted_context(
                    event.unified_msg_origin,
                    self._context_aware_count,
                )
                if context:
                    return context
            elif hasattr(plugin, "get_recent_messages"):
                messages = plugin.get_recent_messages(
                    event.unified_msg_origin,
                    self._context_aware_count,
                )
                if messages:
                    lines = ["[最近的群聊消息]"]
                    for msg in messages:
                        name = "[Bot]" if msg.get("is_bot") else msg.get("sender_name", "未知")
                        lines.append(f"{name}: {msg.get('content', '')}")
                    return "\n".join(lines)
        except Exception as e:
            logger.error(f"[PokeToLLM] 获取 context_aware 上下文失败: {e}")

        return ""

    async def _get_conversation(self, event: AstrMessageEvent):
        """获取当前会话的 conversation 对象"""
        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager

        # 获取当前对话 ID
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            # 如果没有对话，创建一个新的
            cid = await conv_mgr.new_conversation(umo, event.get_platform_id())

        # 获取对话对象
        conversation = await conv_mgr.get_conversation(umo, cid)
        if not conversation:
            # 如果获取失败，再次尝试创建
            cid = await conv_mgr.new_conversation(umo, event.get_platform_id())
            conversation = await conv_mgr.get_conversation(umo, cid)

        return conversation

    async def terminate(self) -> None:
        """清理资源"""
        logger.info(
            f"[PokeToLLM] 插件已终止 | 共响应 {self._poke_count} 次戳一戳"
        )
