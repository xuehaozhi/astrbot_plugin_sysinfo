"""AstrBot 插件：读取服务器实时系统状态并输出。

命令：/sysinfo、/sys、/status、/系统状态、/服务器状态
"""

import asyncio
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
        render_report_image,
        render_text,
    )
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sysinfo_core import (  # type: ignore[no-redef]
        PLUGIN_VERSION,
        build_report,
        collect,
        render_report_image,
        render_text,
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

    async def _handle(self, event: AstrMessageEvent):
        """生成并发送系统状态报告。"""
        if self._opt("admin_only", False):
            try:
                if not event.is_admin():
                    yield event.plain_result("⚠️ 该指令仅管理员可用。")
                    return
            except Exception:
                pass

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
