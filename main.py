"""AstrBot 插件：读取服务器实时系统状态并输出。

命令：/sysinfo、/sys、/status、/系统状态、/服务器状态
"""

import asyncio
import inspect
import json
import os
import sys

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

# 兼容 AstrBot 以模块 / 文件路径两种方式加载本插件
try:
    from .sysinfo_core import (
        PLUGIN_VERSION,
        build_report,
        collect,
        format_context_report,
        render_report_image,
        render_text,
        summarize_history,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sysinfo_core import (  # type: ignore[no-redef]
        PLUGIN_VERSION,
        build_report,
        collect,
        format_context_report,
        render_report_image,
        render_text,
        summarize_history,
    )

# 兼容 LLM 工具注册：新版为 filter.llm_tool，旧版为独立的 llm_tool
_llm_tool = getattr(filter, "llm_tool", None)
if _llm_tool is None:  # pragma: no cover - 旧版本 AstrBot
    try:
        from astrbot.api.event.filter import llm_tool as _llm_tool  # type: ignore[no-redef]
    except Exception:  # noqa: BLE001
        _llm_tool = None


@register(
    "astrbot_plugin_sysinfo",
    "xuehaozhi",
    "读取服务器实时运行状态并输出：CPU 总使用率、每核心使用率、内存占用、磁盘存储等",
    PLUGIN_VERSION,
)
class SysInfoPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.conf = dict(config) if config else {}

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _opt(self, key: str, default):
        value = self.conf.get(key, default)
        return default if value is None else value

    def _options(self) -> dict:
        return {
            "report_style": self._opt("report_style", "detailed"),
            "sample_interval": self._opt("sample_interval", 1.0),
            "show_per_core": self._opt("show_per_core", True),
            "max_cores": self._opt("max_cores", 0),
            "show_disk": self._opt("show_disk", True),
            "disk_paths": self._opt("disk_paths", []),
            "max_disks": self._opt("max_disks", 3),
            "show_net": self._opt("show_net", True),
            "show_net_speed": self._opt("show_net_speed", True),
            "show_temp": self._opt("show_temp", True),
            "max_temp_entries": self._opt("max_temp_entries", 3),
            "show_top_process": self._opt("show_top_process", False),
            "top_process_n": self._opt("top_process_n", 5),
            "font_path": self._opt("font_path", ""),
        }

    # ------------------------------------------------------------------
    # 上下文占用（/context）
    # ------------------------------------------------------------------
    async def _current_provider(self, umo: str):
        """取当前会话使用的对话模型，兼容新旧 AstrBot 的两种取值方式。"""
        getter = getattr(self.context, "get_using_provider_async", None)
        if callable(getter):
            try:
                prov = getter(umo=umo)
                if inspect.isawaitable(prov):
                    prov = await prov
                if prov is not None:
                    return prov
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[sysinfo] 异步获取 Provider 失败：{e}")

        getter = getattr(self.context, "get_using_provider", None)
        if callable(getter):
            try:
                prov = getter(umo)
                while inspect.isawaitable(prov):
                    prov = await prov
                return prov
            except TypeError:
                try:
                    return getter()
                except Exception:  # noqa: BLE001
                    return None
            except Exception:  # noqa: BLE001
                return None
        return None

    def _context_window(self, provider, umo: str) -> int | None:
        """解析模型上下文窗口大小。

        与 AstrBot 的解析顺序一致：Provider 配置 -> 模型元数据 -> 配置兜底值。
        """
        cfg = getattr(provider, "provider_config", None) or {}
        try:
            window = int(cfg.get("max_context_tokens", 0) or 0)
            if window > 0:
                return window
        except (TypeError, ValueError):
            pass

        model = ""
        try:
            model = provider.get_model() if provider is not None else ""
        except Exception:  # noqa: BLE001
            model = ""
        try:
            from astrbot.core.utils.llm_metadata import LLM_METADATAS

            meta = LLM_METADATAS.get(model) if model else None
            if meta:
                ctx = (meta.get("limit") or {}).get("context")
                if ctx:
                    return int(ctx)
        except Exception:  # noqa: BLE001
            pass

        fallback = 128000
        try:
            conf = self.context.get_config(umo) or {}
            node = conf
            for key in ("provider_settings", "agent_runner", "config", "compression"):
                node = node.get(key, {}) if hasattr(node, "get") else {}
            value = node.get("fallback_max_tokens") if hasattr(node, "get") else None
            if value:
                fallback = int(value)
        except Exception:  # noqa: BLE001
            pass
        return fallback

    async def _token_stats(self, cid: str | None) -> dict | None:
        """取本对话最近一次请求的输入/输出 token。

        用最近一次而不是累计值：本指令回答的是“当前上下文占了多少”，
        累计消耗是另一个问题（内置 /stats 已覆盖）。
        取不到（老版本无该表、非 SQLite、无记录）时返回 None，由调用方退回估算。
        """
        if not cid:
            return None
        try:
            from sqlalchemy import func, select

            from astrbot.core.db.po import ProviderStat

            try:
                from sqlmodel import col  # 类型安全的列表达式，与内置 /stats 一致
            except Exception:  # noqa: BLE001

                def col(x):  # type: ignore[misc]
                    return x

        except Exception:  # noqa: BLE001
            return None

        try:
            db = self.context.get_db()
        except Exception:  # noqa: BLE001
            return None

        where = (
            col(ProviderStat.agent_type) == "internal",
            col(ProviderStat.conversation_id) == cid,
        )
        try:
            async with db.get_db() as session:
                last = (
                    await session.execute(
                        select(
                            col(ProviderStat.token_input_other),
                            col(ProviderStat.token_input_cached),
                            col(ProviderStat.token_output),
                        )
                        .where(*where)
                        .order_by(col(ProviderStat.id).desc())
                        .limit(1)
                    )
                ).first()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[sysinfo] 读取 token 统计失败：{e}")
            return None

        if not last:
            return None
        return {
            "input_tokens": int(last[0] or 0) + int(last[1] or 0),
            "output_tokens": int(last[2] or 0),
        }

    async def _context_report(self, event: AstrMessageEvent) -> str:
        umo = getattr(event, "unified_msg_origin", "") or ""

        cid = None
        history: list = []
        manager = getattr(self.context, "conversation_manager", None)
        if manager is not None:
            try:
                cid = await manager.get_curr_conversation_id(umo)
                if cid:
                    conv = await manager.get_conversation(umo, cid)
                    raw = getattr(conv, "history", "") if conv else ""
                    if isinstance(raw, list):
                        history = raw
                    elif isinstance(raw, str) and raw.strip().startswith("["):
                        history = json.loads(raw)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[sysinfo] 读取会话历史失败：{e}")

        summary = summarize_history(history)

        provider = await self._current_provider(umo)
        model = "N/A"
        if provider is not None:
            try:
                model = provider.get_model() or "N/A"
            except Exception:  # noqa: BLE001
                model = "N/A"

        stats = await self._token_stats(cid)
        if stats:
            input_tokens = stats["input_tokens"]
            output_tokens = stats["output_tokens"]
            estimated = False
        else:
            input_tokens = summary["est_input"]
            output_tokens = summary["est_output"]
            estimated = True

        # “使用空间”恒等于输入+输出，两种数据来源都成立，便于用户自行校验
        used = int(input_tokens) + int(output_tokens)

        return format_context_report(
            {
                "model": model,
                "messages": summary["messages"],
                "chars": summary["chars"],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "used_tokens": used,
                "window_tokens": self._context_window(provider, umo) if provider is not None else None,
                "estimated": estimated,
            }
        )

    def _admin_blocked(self, event: AstrMessageEvent) -> bool:
        """配置了 admin_only 时，判断当前用户是否应被拦截。"""
        if not self._opt("admin_only", False):
            return False
        try:
            return not event.is_admin()
        except Exception:  # noqa: BLE001
            return False

    async def _handle(self, event: AstrMessageEvent):
        """生成并发送系统状态报告。"""
        if self._admin_blocked(event):
            yield event.plain_result("⚠️ 该指令仅管理员可用。")
            return

        options = self._options()

        # 图片模式：渲染失败（缺 Pillow / 字体 / IO 异常）一律回退文本
        if self._opt("render_image", False):
            try:
                path = await asyncio.to_thread(render_report_image, options)
                from astrbot.api.message_components import Image

                yield event.chain_result([Image.fromFileSystem(path)])
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[sysinfo] 图片渲染失败，回退为文本：{e}")

        try:
            text = await asyncio.to_thread(build_report, options)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[sysinfo] 读取系统信息失败：{e}")
            text = f"⚠️ 读取服务器状态失败：{e}"

        yield event.plain_result(text)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    @filter.command("context")
    async def context_cmd(self, event: AstrMessageEvent):
        """查看当前模型的上下文使用情况"""
        if self._admin_blocked(event):
            yield event.plain_result("⚠️ 该指令仅管理员可用。")
            return
        try:
            report = await self._context_report(event)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[sysinfo] 读取上下文占用失败：{e}")
            report = f"⚠️ 读取上下文占用失败：{e}"
        yield event.plain_result(report)

    @filter.command("sysinfo")
    async def sysinfo(self, event: AstrMessageEvent):
        """查看服务器 CPU / 内存 / 存储实时状态"""
        async for r in self._handle(event):
            yield r

    @filter.command("sys")
    async def sysinfo_alias_sys(self, event: AstrMessageEvent):
        """查看服务器状态（/sysinfo 别名）"""
        async for r in self._handle(event):
            yield r

    @filter.command("status")
    async def sysinfo_alias_status(self, event: AstrMessageEvent):
        """查看服务器状态（/sysinfo 别名）"""
        async for r in self._handle(event):
            yield r

    @filter.command("系统状态")
    async def sysinfo_alias_cn(self, event: AstrMessageEvent):
        """查看服务器状态（/sysinfo 别名）"""
        async for r in self._handle(event):
            yield r

    @filter.command("服务器状态")
    async def sysinfo_alias_cn2(self, event: AstrMessageEvent):
        """查看服务器状态（/sysinfo 别名）"""
        async for r in self._handle(event):
            yield r

    async def tool_server_status(self, event: AstrMessageEvent):
        """获取服务器当前的实时运行状态。

        用于回答关于服务器 CPU、内存、硬盘、网络或温度等硬件占用情况的问题。
        当用户询问服务器卡不卡、负载高不高、还剩多少内存或硬盘空间时调用此工具。
        """
        options = self._options()
        options["report_style"] = "detailed"
        options["show_top_process"] = True
        try:
            data = await asyncio.to_thread(collect, options)
            return render_text(data, "detailed")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[sysinfo] LLM 工具采集失败：{e}")
            return f"读取服务器状态失败：{e}"

    async def terminate(self):
        """插件卸载时调用。"""
        logger.debug("[sysinfo] 插件已卸载")


# 当前版本支持 LLM 工具时，才把 tool_server_status 注册为大模型可调用工具
if _llm_tool is not None:
    SysInfoPlugin.tool_server_status = _llm_tool(name="get_server_status")(
        SysInfoPlugin.tool_server_status
    )
