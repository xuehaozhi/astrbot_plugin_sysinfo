"""服务器系统状态采集与格式化核心。

本模块刻意不依赖 AstrBot，只依赖 psutil，便于独立运行测试。
"""

from __future__ import annotations

import os
import platform
import socket
import time
from datetime import datetime
from typing import Any

try:
    import psutil
except ImportError:  # pragma: no cover - 由 requirements.txt 保证安装
    psutil = None  # type: ignore[assignment]

NA = "N/A"
BAR_WIDTH = 10
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def human_bytes(num: float, precision: int = 2) -> str:
    """字节数转人类可读字符串（1024 进制）。"""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return NA
    if value < 0:
        return "-"
    for unit in _SIZE_UNITS:
        if abs(value) < 1024.0 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.{precision}f} {unit}"
        value /= 1024.0
    return f"{value:.{precision}f} PB"


def bar(percent: float, width: int = BAR_WIDTH) -> str:
    """用方块字符画一条使用率进度条。"""
    try:
        pct = float(percent)
    except (TypeError, ValueError):
        return "░" * width
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def pct(value: Any, digits: int = 1) -> str:
    """把数值格式化成百分比字符串，取不到就返回 N/A。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return NA
    return f"{v:.{digits}f}%"


def safe(fn, default=None):
    """包装所有可能抛异常的 psutil 调用，单个指标失败不影响整体报告。"""
    if psutil is None:
        return default
    try:
        return fn()
    except Exception:
        return default


def _uptime_str(boot_ts: float, en: bool = False) -> str:
    secs = max(0, int(time.time() - boot_ts))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if en:  # 图片模式在无中文字体时使用
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m {seconds}s"
        return f"{minutes}m {seconds}s"
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分 {seconds} 秒"
    return f"{minutes} 分 {seconds} 秒"


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def _sample_cpu(interval: float) -> tuple[float | None, list[float]]:
    """采样 CPU 使用率。

    psutil.cpu_percent 是“两次调用之间的差值”，所以先各调用一次建立基准，
    睡眠 interval 后再取值，得到的是该窗口内的平均使用率。
    """
    if psutil is None:
        return None, []
    try:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
        time.sleep(max(0.0, interval))
        total = psutil.cpu_percent(interval=None)
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        return total, list(per_core)
    except Exception:
        return None, []


# 伪文件系统：这些不是真实磁盘，占满了视图也毫无意义
_PSEUDO_FS = frozenset(
    {
        "tmpfs", "devtmpfs", "sysfs", "proc", "devtmpfs", "cgroup", "cgroup2",
        "devpts", "mqueue", "debugfs", "tracefs", "securityfs", "fusectl",
        "binfmt_misc", "nsfs", "pstore", "configfs", "selinuxfs", "autofs",
        "hugetlbfs", "ramfs", "overlayfs", "squashfs", "iso9660", "none",
    }
)


def _should_skip_mount(mountpoint: str, fstype: str) -> bool:
    """判断该挂载点是否应当忽略。"""
    if fstype and fstype.lower() in _PSEUDO_FS:
        return True
    low = mountpoint.lower()
    if low.startswith(("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker")):
        return True
    try:
        # Docker 会把单个文件挂进来（/etc/hostname、/etc/resolv.conf），不是分区
        # Windows 上的空光驱会抛 OSError，同样跳过
        if not os.path.isdir(mountpoint):
            return True
    except OSError:
        return True
    return False


def _default_disk_paths(max_disks: int) -> list[str]:
    """挑选要展示的磁盘挂载点，自动剔除伪目录、重复设备和 bind mount。"""
    if psutil is None:
        return []

    try:
        parts = list(psutil.disk_partitions(all=False))
    except Exception:
        parts = []

    root = "/" if os.sep == "/" else os.path.splitdrive(os.getcwd())[0] + os.sep
    picked: list[str] = []
    seen_mp: set[str] = set()
    seen_dev: set[str] = set()
    seen_size: set[tuple[int, int]] = set()

    # 根目录/当前盘符排最前，其余按路径长度升序
    ordered = sorted(parts, key=lambda p: (p.mountpoint != root, len(p.mountpoint), p.mountpoint))

    for part in ordered:
        mp = part.mountpoint
        if not mp or mp in seen_mp:
            continue
        if _should_skip_mount(mp, part.fstype):
            continue
        usage = safe(lambda p=mp: psutil.disk_usage(p), None)
        if usage is None:
            continue
        dev = part.device or ""
        total_mb = round(getattr(usage, "total", 0) / 1048576)
        used_mb = round(getattr(usage, "used", 0) / 1048576)
        # 同一设备的多个 bind mount（Docker 里很常见）只保留一个；
        # 容量与已用完全相同的不同挂载点，也视为同一块盘。
        if dev and dev in seen_dev:
            continue
        if (total_mb, used_mb) in seen_size:
            continue
        seen_dev.add(dev)
        seen_size.add((total_mb, used_mb))
        seen_mp.add(mp)
        picked.append(mp)

    if not picked and root and os.path.isdir(root):
        picked = [root]
    return picked[: max(1, max_disks)]


def collect(options: dict[str, Any] | None = None) -> dict[str, Any]:
    """采集一份完整的系统快照。任何一项取不到都退化为 None，交给渲染层处理。"""
    opt = options or {}
    interval = float(opt.get("sample_interval", 1.0) or 0)
    show_per_core = bool(opt.get("show_per_core", True))
    show_disk = bool(opt.get("show_disk", True))
    show_net = bool(opt.get("show_net", True))
    show_temp = bool(opt.get("show_temp", True))
    show_top_proc = bool(opt.get("show_top_process", False))
    max_disks = int(opt.get("max_disks", 3) or 3)
    disk_paths = opt.get("disk_paths") or []
    temp_limit = int(opt.get("max_temp_entries", 3) or 3)
    max_cores = int(opt.get("max_cores", 0) or 0)

    data: dict[str, Any] = {
        "ok": psutil is not None,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sample_interval": interval,
    }

    # ---- CPU ----
    cpu_total, per_core = _sample_cpu(interval) if show_per_core or True else (None, [])
    cores_physical = safe(lambda: psutil.cpu_count(logical=False), None)
    cores_logical = safe(lambda: psutil.cpu_count(logical=True), None)
    freq = safe(lambda: psutil.cpu_freq(), None)
    load = safe(lambda: os.getloadavg(), None) if hasattr(os, "getloadavg") else None
    cpu_model = safe(lambda: platform.processor(), "") or ""

    cpu_cores: list[float] = []
    if show_per_core:
        cpu_cores = per_core
        if max_cores and len(cpu_cores) > max_cores:
            # 折叠中间核心，避免核心太多刷屏
            head = cpu_cores[:max_cores]
            cpu_cores_display = head
            cpu_cores_hidden = len(cpu_cores) - max_cores
        else:
            cpu_cores_display = cpu_cores
            cpu_cores_hidden = 0
    else:
        cpu_cores_display = []
        cpu_cores_hidden = len(per_core)

    data["cpu"] = {
        "total": cpu_total,
        "cores": cpu_cores_display,
        "hidden_cores": cpu_cores_hidden,
        "all_core_avg": (sum(per_core) / len(per_core)) if per_core else None,
        "physical": cores_physical,
        "logical": cores_logical,
        "freq_current": getattr(freq, "current", None),
        "freq_max": getattr(freq, "max", None),
        "load": list(load) if load else None,
        "model": cpu_model,
    }

    # ---- 内存 ----
    vm = safe(lambda: psutil.virtual_memory(), None)
    swap = safe(lambda: psutil.swap_memory(), None)
    data["memory"] = {
        "percent": getattr(vm, "percent", None),
        "used": getattr(vm, "used", None),
        "total": getattr(vm, "total", None),
        "available": getattr(vm, "available", None),
    }
    data["swap"] = {
        "percent": getattr(swap, "percent", None),
        "used": getattr(swap, "used", None),
        "total": getattr(swap, "total", None),
    }

    # ---- 磁盘 ----
    disks: list[dict[str, Any]] = []
    if show_disk:
        paths = list(disk_paths) if disk_paths else _default_disk_paths(max_disks)
        for p in paths[: max(1, max_disks)]:
            usage = safe(lambda p=p: psutil.disk_usage(p), None)
            if usage is None:
                disks.append({"path": p, "percent": None, "used": None, "total": None})
                continue
            io = safe(lambda p=p: psutil.disk_io_counters(perdisk=False), None)
            disks.append(
                {
                    "path": p,
                    "percent": getattr(usage, "percent", None),
                    "used": getattr(usage, "used", None),
                    "total": getattr(usage, "total", None),
                    "free": getattr(usage, "free", None),
                    "read": getattr(io, "read_bytes", None) if io else None,
                    "write": getattr(io, "write_bytes", None) if io else None,
                }
            )
    data["disk"] = disks

    # ---- 网络 ----
    net = None
    if show_net:
        counters = safe(lambda: psutil.net_io_counters(), None)
        net = {
            "sent": getattr(counters, "bytes_sent", None) if counters else None,
            "recv": getattr(counters, "bytes_recv", None) if counters else None,
        }
    data["net"] = net

    # ---- 温度 ----
    temps: list[tuple[str, str, float]] = []
    if show_temp:
        raw = safe(lambda: psutil.sensors_temperatures(), {}) or {}
        for chip, entries in raw.items():
            for e in entries:
                temps.append((chip, getattr(e, "label", "") or chip, getattr(e, "current", None)))
        temps = [t for t in temps if t[2] is not None][:temp_limit]
    data["temp"] = temps

    # ---- 系统 ----
    boot = safe(lambda: psutil.boot_time(), None)
    data["system"] = {
        "os": f"{platform.system()} {platform.release()}",
        "version": platform.version(),
        "arch": platform.machine(),
        "hostname": safe(lambda: socket.gethostname(), NA),
        "python": platform.python_version(),
        "boot_time": datetime.fromtimestamp(boot).strftime("%Y-%m-%d %H:%M:%S") if boot else None,
        "uptime": _uptime_str(boot) if boot else None,
        "uptime_en": _uptime_str(boot, en=True) if boot else None,
        "process_count": safe(lambda: len(psutil.pids()), None),
    }

    # ---- 进程 Top ----
    if show_top_proc:
        top_n = int(opt.get("top_process_n", 5) or 5)
        procs: list[dict[str, Any]] = []
        for p in safe(lambda: list(psutil.process_iter(["pid", "name", "memory_percent"])), []) or []:
            try:
                info = p.info  # type: ignore[union-attr]
                mp = info.get("memory_percent") or 0
                procs.append({"pid": info.get("pid"), "name": info.get("name") or "?", "mem": mp})
            except Exception:
                continue
        procs.sort(key=lambda x: x["mem"], reverse=True)
        data["procs"] = procs[:top_n]
    else:
        data["procs"] = []

    return data


# --------------------------------------------------------------------------
# 渲染：concise（贴合示例格式）
# --------------------------------------------------------------------------
def render_concise(data: dict[str, Any]) -> str:
    cpu = data.get("cpu", {})
    lines = ["#System状态"]

    cpu_val = cpu.get("total")
    lines.append(f"-CPU：{pct(cpu_val)} 使用")

    for i, c in enumerate(cpu.get("cores", []), start=1):
        lines.append(f"-Core{i}：{pct(c)} 使用")
    if cpu.get("hidden_cores"):
        lines.append(f"-Core…：其余 {cpu['hidden_cores']} 个核心已折叠")

    mem = data.get("memory", {})
    lines.append(
        f"-RAM：{pct(mem.get('percent'))} 使用（{human_bytes(mem.get('used'))} / {human_bytes(mem.get('total'))}）"
    )

    disks = data.get("disk", [])
    if disks:
        for d in disks:
            if d.get("percent") is None:
                lines.append(f"-存储[{d.get('path')}]：{NA} 占用")
            else:
                lines.append(
                    f"-存储[{d.get('path')}]：{pct(d.get('percent'))} 占用"
                    f"（{human_bytes(d.get('used'))} / {human_bytes(d.get('total'))}）"
                )
    else:
        lines.append(f"-存储：{NA} 占用 / {NA}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# 渲染：detailed（带进度条与附加模块）
# --------------------------------------------------------------------------
def render_detailed(data: dict[str, Any]) -> str:
    if not data.get("ok"):
        return "⚠️ 缺少依赖 psutil，无法读取系统信息。请在插件目录下安装 requirements.txt 中的依赖。"

    cpu, sysd = data["cpu"], data["system"]
    lines: list[str] = ["🖥 服务器状态"]

    # 头部信息
    head = f"系统：{sysd.get('os')}（{sysd.get('arch')}）"
    if sysd.get("uptime"):
        head += f"\n运行：{sysd['uptime']}"
    if cpu.get("model"):
        head += f"\nCPU：{cpu['model']}"
    if cpu.get("physical") and cpu.get("logical"):
        head += f"\n规格：{cpu['physical']} 核 / {cpu['logical']} 线程"
    lines.append(head)

    # CPU
    lines.append("────────────────")
    lines.append(f"CPU 总使用率  {pct(cpu.get('total')).rjust(6)}  {bar(cpu.get('total') or 0)}")
    if cpu.get("freq_current"):
        freq_txt = f"{cpu['freq_current'] / 1000:.2f} GHz"
        if cpu.get("freq_max"):
            freq_txt += f" / {cpu['freq_max'] / 1000:.2f} GHz"
        lines.append(f"CPU 频率      {freq_txt}")
    for i, c in enumerate(cpu.get("cores", []), start=1):
        lines.append(f"  Core {i:<3}   {pct(c).rjust(6)}  {bar(c)}")
    if cpu.get("hidden_cores"):
        lines.append(f"  … 其余 {cpu['hidden_cores']} 个核心已折叠（可调整 max_cores）")
    if cpu.get("load"):
        lines.append(f"负载 1/5/15m  {'  '.join(f'{x:.2f}' for x in cpu['load'])}")

    # 内存
    mem, swap = data["memory"], data["swap"]
    lines.append("────────────────")
    lines.append(
        f"RAM          {pct(mem.get('percent')).rjust(6)}  {bar(mem.get('percent') or 0)}"
        f"  {human_bytes(mem.get('used'))} / {human_bytes(mem.get('total'))}"
    )
    if mem.get("available") is not None:
        lines.append(f"可用内存      {human_bytes(mem['available'])}")
    if swap.get("total"):
        lines.append(
            f"Swap         {pct(swap.get('percent')).rjust(6)}  {bar(swap.get('percent') or 0)}"
            f"  {human_bytes(swap.get('used'))} / {human_bytes(swap.get('total'))}"
        )

    # 磁盘
    lines.append("────────────────")
    for d in data.get("disk", []):
        name = str(d.get("path")).ljust(8)
        if d.get("percent") is None:
            lines.append(f"存储 {name} 无法读取（路径不存在或无权限）")
        else:
            lines.append(
                f"存储 {name}{pct(d.get('percent')).rjust(6)}  {bar(d.get('percent') or 0)}"
                f"  {human_bytes(d.get('used'))} / {human_bytes(d.get('total'))}"
            )

    # 网络
    net = data.get("net")
    if net and (net.get("sent") is not None or net.get("recv") is not None):
        lines.append("────────────────")
        lines.append(f"网络  ↑ {human_bytes(net.get('sent'))}   ↓ {human_bytes(net.get('recv'))}")

    # 温度
    if data.get("temp"):
        lines.append("────────────────")
        for chip, label, cur in data["temp"]:
            lines.append(f"温度  {chip}.{label}  {cur:.1f}°C")

    # 进程
    if data.get("procs"):
        lines.append("────────────────")
        lines.append("内存占用 Top 进程：")
        for p in data["procs"]:
            lines.append(f"  · {str(p['name'])[:18]}（PID {p['pid']}） {p['mem']:.1f}%")

    lines.append("────────────────")
    extra = []
    if sysd.get("process_count") is not None:
        extra.append(f"进程数 {sysd['process_count']}")
    extra.append(f"主机名 {sysd.get('hostname')}")
    extra.append(f"采样 {data.get('sample_interval', 0)}s")
    lines.append(" | ".join(extra))
    lines.append(f"采集于 {data.get('collected_at')}")

    return "\n".join(lines)


def render_text(data: dict[str, Any], style: str = "detailed") -> str:
    """按风格渲染文本报告。"""
    if not data.get("ok"):
        return "⚠️ 缺少依赖 psutil，无法读取系统信息。请在插件目录下安装 requirements.txt 中的依赖。"
    if style == "concise":
        return render_concise(data)
    return render_detailed(data)


def build_report(options: dict[str, Any] | None = None) -> str:
    """一步到位：采集 + 渲染文本。"""
    opt = dict(options or {})
    style = str(opt.pop("report_style", "detailed") or "detailed")
    return render_text(collect(opt), style)


# --------------------------------------------------------------------------
# 图片渲染（可选，需要 Pillow）
# --------------------------------------------------------------------------
_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/usr/share/fontconfig",
    "~/.fonts",
    "~/.local/share/fonts",
    "/Library/Fonts",
    "/System/Library/Fonts",
)
# 常见中文字体的文件名关键词，用于给候选字体排序
_CJK_FONT_HINTS = (
    "noto", "wqy", "sourcehan", "source-han", "droidsansfallback", "simhei",
    "simsun", "msyh", "pingfang", "heiti", "songti", "cjk", "micro", "hei",
    "song", "kai", "yahei",
)
_PROBE_CHAR = "状"  # 用于判断字体是否含中文字形
# 私用区字符，任何字体都不该有，用于取得“缺字形方框(tofu)”的形状作为对照
_TOFU_CHARS = ("", "", "")


def _iter_font_files(limit: int = 400):
    """遍历系统字体目录，优先返回名字像中文字体的文件。"""
    dirs = _FONT_DIRS
    if os.name == "nt":
        dirs = (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),) + dirs

    found: list[str] = []
    for d in dirs:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            continue
        try:
            for root, _sub, files in os.walk(d):
                for f in files:
                    if f.lower().endswith((".ttf", ".ttc", ".otf", ".otc")):
                        found.append(os.path.join(root, f))
                        if len(found) >= limit:
                            return found
        except OSError:
            continue

    base_cache = [os.path.basename(p).lower() for p in found]
    scored = sorted(
        range(len(found)),
        key=lambda i: (not any(h in base_cache[i] for h in _CJK_FONT_HINTS), base_cache[i]),
    )
    return [found[i] for i in scored]


def _font_supports_cjk(font) -> bool:
    """字体能否真正渲染汉字。

    缺字形时 PIL 并不会报错，而是画出一个空心方框（tofu），
    所以不能只看 bbox 是否非空 —— 要拿“字体里必定不存在”的私用区字符
    作为方框形状的参照物来对照。
    """
    try:
        probe = font.getbbox(_PROBE_CHAR)
        refs = [font.getbbox(c) for c in _TOFU_CHARS]
    except Exception:
        return False
    if not probe or probe[2] <= 0 or probe[3] <= 0:
        return False
    # 所有私用区字符都渲染不出(row)，无法对照，此时保守相信 probe 非空
    if all(not r or (r[2] <= 0 and r[3] <= 0) for r in refs):
        return True
    return all(probe != r for r in refs)


def _load_font(size: int, bold: bool = False, custom_path: str | None = None):
    """加载字体，返回 (font, 是否支持中文)。"""
    from PIL import ImageFont  # 局部导入：未安装 Pillow 时不影响文本功能

    candidates: list[str] = []
    if custom_path:
        candidates.append(os.path.expanduser(custom_path))

    preferred: list[str] = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        preferred += [
            os.path.join(windir, "Fonts", "msyhbd.ttc" if bold else "msyh.ttc"),
            os.path.join(windir, "Fonts", "msyhl.ttc"),
            os.path.join(windir, "Fonts", "simhei.ttf"),
        ]
    else:
        preferred += [
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ]

    candidates += preferred
    try:
        candidates += _iter_font_files()
    except Exception:
        pass

    latin_fallback = None
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            font = ImageFont.truetype(path, size)
        except Exception:
            continue
        if _font_supports_cjk(font):
            return font, True
        if latin_fallback is None:
            latin_fallback = font

    return (latin_fallback or ImageFont.load_default()), False


def render_report_image(options: dict[str, Any] | None = None, save_path: str | None = None) -> str:
    """把详细报告渲染成一张 PNG 卡片，返回文件路径。"""
    from PIL import Image, ImageDraw  # 局部导入

    opt = dict(options or {})
    opt.pop("report_style", None)
    data = collect(opt)

    W, PAD = 780, 28
    BG = (247, 248, 250)
    CARD = (255, 255, 255)
    LINE = (226, 230, 236)
    TITLE = (23, 26, 33)
    TEXT = (60, 66, 78)
    SUB = (120, 128, 142)
    GREEN = (46, 172, 96)
    AMBER = (234, 165, 42)
    RED = (222, 68, 55)
    BLUE = (45, 122, 224)

    f_title, cjk = _load_font(24, bold=True, custom_path=opt.get("font_path"))
    f_head, _ = _load_font(17, bold=True, custom_path=opt.get("font_path"))
    f_text, _ = _load_font(15, custom_path=opt.get("font_path"))
    f_small, _ = _load_font(13, custom_path=opt.get("font_path"))

    # 系统缺中文字体时（精简版 Docker 镜像很常见），整体切换为英文标签，
    # 避免数字是对的、中文却全是方框。
    def L(zh: str, en: str) -> str:
        return zh if cjk else en

    T_TITLE = L("System 状态", "System Status")
    T_UPTIME = L("已运行", "Uptime")
    T_UPDATED = L("采集于", "Updated")
    T_CPU = "CPU"
    T_TOTAL = L("总使用率", "Total")
    T_MEM = L("内存", "Memory")
    T_DISK = L("存储", "Disk")
    T_NET = L("网络", "Network")
    T_TEMP = L("温度", "Temperature")
    T_HIDDEN = L("其余 {} 个核心已折叠", "{} more cores omitted")

    def color_of(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return SUB
        if v >= 85:
            return RED
        if v >= 65:
            return AMBER
        return GREEN

    rows: list[tuple[str, dict]] = []

    def add_row(kind: str, **kw):
        rows.append((kind, kw))

    sysd, cpu = data["system"], data["cpu"]
    add_row("meta", text=f"{sysd.get('os')} · {sysd.get('arch')} · {sysd.get('hostname')}")
    add_row(
        "meta",
        text=f"{T_UPTIME} {sysd.get('uptime_en' if not cjk else 'uptime') or NA} · {T_UPDATED} {data.get('collected_at')}",
    )
    if cpu.get("model"):
        add_row(
            "meta",
            text=f"{cpu['model']} · {cpu.get('physical') or NA}C / {cpu.get('logical') or NA}T",
        )

    add_row("section", text=T_CPU)
    add_row("stat", label=T_TOTAL, value=cpu.get("total"), right=pct(cpu.get("total")))
    for i, c in enumerate(cpu.get("cores", []), start=1):
        add_row("stat", label=f"Core {i}", value=c, right=pct(c))
    if cpu.get("hidden_cores"):
        add_row("meta", text=T_HIDDEN.format(cpu["hidden_cores"]))

    add_row("section", text=T_MEM)
    mem = data["memory"]
    add_row(
        "stat",
        label="RAM",
        value=mem.get("percent"),
        right=f"{pct(mem.get('percent'))}   {human_bytes(mem.get('used'))} / {human_bytes(mem.get('total'))}",
    )
    if data.get("disk"):
        add_row("section", text=T_DISK)
        for d in data["disk"]:
            add_row(
                "stat",
                label=str(d.get("path")),
                value=d.get("percent"),
                right=f"{pct(d.get('percent'))}   {human_bytes(d.get('used'))} / {human_bytes(d.get('total'))}",
            )

    if data.get("net"):
        n = data["net"]
        add_row("section", text=T_NET)
        add_row("meta", text=f"↑ {human_bytes(n.get('sent'))}   ↓ {human_bytes(n.get('recv'))}")
    if data.get("temp"):
        add_row("section", text=T_TEMP)
        for chip, label, cur in data["temp"]:
            add_row("meta", text=f"{chip} · {label}：{cur:.1f} °C")

    # 按真实字体尺寸计算行高，避免内容被卡片底边裁切
    probe = ImageDraw.Draw(Image.new("RGB", (W, 10)))

    def line_h(font, gap: int) -> int:
        bbox = probe.textbbox((0, 0), "Ágjyp", font=font)
        return (bbox[3] - bbox[1]) + gap

    h_title = line_h(f_title, 14)
    h_head = line_h(f_head, 22)
    h_stat = line_h(f_text, 12)
    h_meta = line_h(f_small, 9)

    H = PAD + 12
    H += h_title + 12  # 标题 + 分隔线
    for kind, _ in rows:
        H += h_stat if kind == "stat" else (h_head if kind == "section" else h_meta)
    H += 18 + h_meta + PAD  # 底部分隔线 + 落款 + 留白

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([PAD - 12, PAD - 12, W - PAD + 12, H - PAD + 12], fill=CARD, outline=LINE, width=1)

    x, y = PAD, PAD
    draw.rectangle([x, y + 4, x + 5, y + h_title - 8], fill=BLUE)  # 标题色条
    draw.text((x + 14, y), T_TITLE, font=f_title, fill=TITLE)
    y += h_title
    draw.line([x, y, W - PAD, y], fill=LINE, width=1)
    y += 12

    for kind, kw in rows:
        if kind == "meta":
            draw.text((x, y), kw["text"], font=f_small, fill=SUB)
            y += h_meta
        elif kind == "section":
            y += 8
            draw.rectangle([x, y + 3, x + 4, y + h_head - 12], fill=BLUE)
            draw.text((x + 12, y), kw["text"], font=f_head, fill=TITLE)
            y += h_head
        else:  # stat
            value = kw.get("value")
            draw.text((x, y), kw["label"], font=f_text, fill=TEXT)
            bw, bh = 168, 10
            bx = x + 150
            by = y + max(0, (h_stat - bh) // 2 - 2)
            draw.rectangle([bx, by, bx + bw, by + bh], fill=(235, 238, 243))
            if value is not None:
                try:
                    w = max(2, int(float(value) / 100 * bw))
                    draw.rectangle([bx, by, bx + w, by + bh], fill=color_of(value))
                except (TypeError, ValueError):
                    pass
            draw.text((bx + bw + 12, y + 2), kw.get("right") or NA, font=f_small, fill=TEXT)
            y += h_stat

    y += 6
    draw.line([x, y, W - PAD, y], fill=LINE, width=1)
    draw.text((x, y + 10), "SysInfo · AstrBot Plugin", font=f_small, fill=SUB)

    if save_path is None:
        import tempfile

        save_path = os.path.join(tempfile.gettempdir(), f"astrbot_sysinfo_{int(time.time())}.png")
    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    img.save(save_path, format="PNG")
    return save_path


if __name__ == "__main__":
    print(build_report({"report_style": "detailed"}))
