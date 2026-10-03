"""跨平台适配层。

把"这个平台有什么"集中到一处，别处只管调，不再到处写 ``if windows``。

覆盖四类差异：

1. **系统信息** —— 内存 / 运行时间 / CPU 型号 / 磁盘 / 负载
   Linux 读 ``/proc``，macOS 走 ``sysctl``，Windows 走 ``ctypes``
2. **可执行文件后缀** —— Windows 要 ``.exe``
3. **权限位** —— Windows 没有 SUID/SGID 这套概念
4. **伪终端能力** —— ``pty`` / ``fork`` 是 POSIX 专有

所有函数的原则：**拿不到就返回占位符，绝不抛异常**。
系统信息用来装点门面，为它崩掉整个 shell 不值得。
"""

from __future__ import annotations

import ctypes
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time

__all__ = [
    "IS_WINDOWS", "IS_MACOS", "IS_LINUX", "IS_POSIX",
    "exe_name", "have_pty", "supports_suid", "setup_console_encoding",
    "meminfo", "uptime_text", "cpu_model", "disk_text", "load_text",
    "loopback_bytes", "disk_bytes", "split_command", "machine_tag",
    "human_size",
]

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
IS_POSIX = os.name == "posix"

#: 拿不到信息时的占位符
UNKNOWN = "?"


# ===========================================================================
# 控制台编码
# ===========================================================================
def setup_console_encoding() -> bool:
    """Windows 控制台默认是 cp1252/cp936，输出中文直接 UnicodeEncodeError。

    ★ 这个 bug 在 Linux 上**永远看不到** —— 是交叉编译出 exe、真跑一遍
      才炸出来的：``rich`` 走的是 Win32 控制台 API，用的是**当前代码页**，
      代码页不对就编码失败，整个 shell 起不来。

    做两件事：

    1. 把控制台输入/输出代码页切成 UTF-8（65001）
    2. 重绑 ``sys.stdout``/``stderr`` 到 UTF-8，并且 ``errors="replace"`` ——
       万一代码页切不动（输出被重定向、老系统），也只是显示成问号，
       而不是抛异常把程序带走

    非 Windows 上是空操作，返回 False。
    """
    if not IS_WINDOWS:
        return False

    switched = False
    try:
        kernel32 = ctypes.windll.kernel32
        # 65001 = CP_UTF8
        if kernel32.SetConsoleOutputCP(65001):
            switched = True
        kernel32.SetConsoleCP(65001)
    except (OSError, AttributeError):
        pass

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 被重定向成非 TextIOWrapper、或已经关了 —— 都不该致命
            pass
    return switched


# ===========================================================================
# 能力探测
# ===========================================================================
def machine_tag() -> str:
    """拼文件名用的架构标签，**跨平台统一**。

    ★ ``platform.machine()`` 各平台叫法不一样，同一种架构好几个名字：

    ==========  ============
    Linux        ``x86_64``
    Windows      ``AMD64``
    macOS(Intel) ``x86_64``
    macOS(ARM)   ``arm64``
    Linux(ARM)   ``aarch64``
    ==========  ============

    vendor 里的产物是按 ``<系统>-<架构>`` 命名的，直接拿
    ``platform.machine()`` 拼，Windows 上会去找
    ``fastfetch-windows-AMD64.exe`` —— **永远找不到**，
    只能悄悄退回纯 Python 实现（用户看不出来，只觉得信息变少了）。

    统一成小写的规范名。
    """
    machine = platform.machine().lower()
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
        "i386": "i686",
        "i486": "i686",
        "i586": "i686",
    }.get(machine, machine)


def exe_name(base: str) -> str:
    """按平台补上可执行文件后缀。

    ``exe_name("jiahaoshell")`` -> ``jiahaoshell.exe``（Windows）/ 原样（其它）
    """
    if IS_WINDOWS and not base.lower().endswith(".exe"):
        return base + ".exe"
    return base


def have_pty() -> bool:
    """这个平台能不能开伪终端。

    依赖 ``pty`` + ``termios`` + ``fcntl``（POSIX 专有）和 ``os.fork``。
    Windows 得走 ConPTY，那是另一套 API，目前没实现。
    """
    if not IS_POSIX or not hasattr(os, "fork"):
        return False
    try:
        import fcntl  # noqa: F401
        import pty  # noqa: F401
        import termios  # noqa: F401
    except ImportError:
        return False
    return True


def supports_suid() -> bool:
    """文件系统是否支持 SUID/SGID 位。

    Windows 的权限模型是 ACL，没有这三个位 —— 硬查只会永远查不到，
    不如让调用方跳过这一项，别在报告里写"未发现 SUID"误导人。
    """
    if IS_WINDOWS:
        return False
    return all(hasattr(__import__("stat"), name)
               for name in ("S_ISUID", "S_ISGID"))


# ===========================================================================
# 命令行拆分
# ===========================================================================
def split_command(command: str) -> list[str]:
    r"""把命令行拆成 argv，**按平台**选规则。

    ★ Windows 上必须 ``posix=False``。

    ``shlex.split`` 默认走 POSIX 规则，把反斜杠当转义符 ——

        >>> shlex.split("Z:\\tmp\\jiahaoshell.exe")
        ['Z:tmpjiahaoshell.exe']          # 路径被吃掉了

    Windows 的路径全是反斜杠，于是子进程根本起不来。
    这个 bug 在 Linux 上永远看不到，是**交叉编译出 exe、真跑一遍**才炸出来的。

    ``posix=False`` 的副作用：引号会留在 token 里，所以再手工剥一层。
    """
    if not IS_WINDOWS:
        return shlex.split(command)

    argv = shlex.split(command, posix=False)
    out = []
    for token in argv:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        out.append(token)
    return out


# ===========================================================================
# 存储
# ===========================================================================
def human_size(n: float) -> str:
    """字节数转人类可读。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def disk_text(path: str = "/") -> str:
    """``已用 / 总量``。

    用 ``shutil.disk_usage`` 而不是 ``os.statvfs`` —— 后者 Windows 没有。
    """
    try:
        total, _used, free = shutil.disk_usage(path)
    except (OSError, ValueError):
        return UNKNOWN
    used = total - free
    return f"{used / 1024 ** 3:.0f} GiB / {total / 1024 ** 3:.0f} GiB"


def disk_bytes(path: str = "/"):
    """``(已用, 总量, 可用)`` 字节数；拿不到返回 None。"""
    try:
        total, _used, free = shutil.disk_usage(path)
    except (OSError, ValueError):
        return None
    return total - free, total, free


# ===========================================================================
# 内存
# ===========================================================================
def _meminfo_linux():
    """/proc/meminfo 的单位是 **kB**，这里统一换算成字节再返回。"""
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, value = line.partition(":")
            info[key.strip()] = int(value.split()[0])
    total = info.get("MemTotal", 0) * 1024
    avail = info.get("MemAvailable", info.get("MemTotal", 0)) * 1024
    return total - avail, total


def _meminfo_macos():
    """macOS 没有 /proc，走 sysctl + vm_stat。"""
    def _sysctl(name: str) -> int:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True,
                             text=True, timeout=5)
        return int(out.stdout.strip())

    total = _sysctl("hw.memsize")
    # vm_stat 的页大小不一定是 4096，得自己问
    page = _sysctl("hw.pagesize")
    stats = subprocess.run(["vm_stat"], capture_output=True, text=True,
                           timeout=5).stdout
    free_pages = 0
    for line in stats.splitlines():
        # 形如 "Pages free:                    123456."
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            continue
        if key in ("pages free", "pages inactive", "pages speculative"):
            free_pages += int(digits)
    return total - free_pages * page, total


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _meminfo_windows():
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx 失败")
    total = int(status.ullTotalPhys)
    return total - int(status.ullAvailPhys), total


def meminfo() -> tuple[str, str]:
    """``(已用, 总量)``，带单位；拿不到返回 ``('?', '?')``。"""
    try:
        if IS_LINUX:
            used, total = _meminfo_linux()
        elif IS_MACOS:
            used, total = _meminfo_macos()
        elif IS_WINDOWS:
            used, total = _meminfo_windows()
        else:
            return UNKNOWN, UNKNOWN
    except (OSError, ValueError, IndexError, KeyError, AttributeError,
            subprocess.SubprocessError):
        return UNKNOWN, UNKNOWN
    gib = 1024 ** 3
    return f"{used / gib:.1f} GiB", f"{total / gib:.1f} GiB"


# ===========================================================================
# 运行时间
# ===========================================================================
def _uptime_seconds() -> float | None:
    if IS_LINUX:
        with open("/proc/uptime") as fh:
            return float(fh.read().split()[0])

    if IS_MACOS:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"],
                             capture_output=True, text=True, timeout=5).stdout
        # 形如 { sec = 1699999999, usec = 0 } ...
        digits = ""
        for ch in out.partition("sec =")[2]:
            if ch.isdigit():
                digits += ch
            elif digits:
                break
        if not digits:
            return None
        return max(0.0, time.time() - int(digits))

    if IS_WINDOWS:
        # ★ 必须显式声明 restype。
        #
        #   ctypes 不声明就按 **32 位有符号 int** 读返回值，而
        #   GetTickCount64 返回的是 64 位毫秒数 —— 开机超过 24.8 天
        #   （2^31 ms）就溢出成负数。真机上亲眼见过
        #   "Uptime: -4 天 17 小时 21 分"。
        #
        #   另外这里不能按名字猜"64 位所以用 c_longlong"：
        #   它是**无符号**的，用有符号一样会在 2^63 之后出错（虽然那时
        #   人类已经不用 Windows 了，但类型该对就得对）。
        _kernel32 = ctypes.windll.kernel32
        _kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        _kernel32.GetTickCount64.argtypes = []
        return _kernel32.GetTickCount64() / 1000.0

    return None


def uptime_text() -> str:
    """``「N 天 N 小时 N 分」``；拿不到返回 ``?``。"""
    try:
        secs = _uptime_seconds()
    except (OSError, ValueError, IndexError, AttributeError,
            subprocess.SubprocessError):
        return UNKNOWN
    if secs is None:
        return UNKNOWN
    days, rem = divmod(int(secs), 86400)
    hours, rem = divmod(rem, 3600)
    return f"{days} 天 {hours} 小时 {rem // 60} 分"


# ===========================================================================
# CPU
# ===========================================================================
def cpu_model() -> str:
    """CPU 型号；拿不到返回 ``?``。"""
    try:
        if IS_LINUX:
            with open("/proc/cpuinfo") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()

        elif IS_MACOS:
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5)
            if out.stdout.strip():
                return out.stdout.strip()
            # Apple Silicon 没有 machdep.cpu.brand_string
            out = subprocess.run(["sysctl", "-n", "hw.model"],
                                 capture_output=True, text=True, timeout=5)
            if out.stdout.strip():
                return out.stdout.strip()

        elif IS_WINDOWS:
            import platform as _p
            name = _p.processor()
            if name:
                return name
            # 退而求其次：注册表里的型号字符串
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                return str(winreg.QueryValueEx(key,
                                               "ProcessorNameString")[0]).strip()
    except (OSError, ValueError, IndexError, ImportError, AttributeError,
            subprocess.SubprocessError):
        pass

    import platform as _p
    return _p.processor() or UNKNOWN


def load_text() -> str | None:
    """``1 分钟 / 5 分钟 / 15 分钟`` 负载；平台不支持返回 None。

    ``os.getloadavg`` 在 Windows 上不存在 —— 那是个 Unix 概念。
    """
    if not hasattr(os, "getloadavg"):
        return None
    try:
        return "  ".join(f"{v:.2f}" for v in os.getloadavg())
    except (OSError, ValueError):
        return None


# ===========================================================================
# 回环流量
# ===========================================================================
def loopback_bytes():
    """回环网卡的 ``(收到, 发出)`` 字节数；拿不到返回 None。

    ``ddos`` 那段表演的收尾要拿它摆一个**真实**数字（号称打了 420 万包/秒，
    回环计数其实几乎不动 —— 笑点全在这个反差上）。所以这里必须真读，
    读不到就返回 None 让调用方少打一行，**不能编**。
    """
    try:
        if IS_LINUX:
            return _loopback_linux()
        if IS_MACOS:
            return _loopback_macos()
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return None


def _loopback_linux():
    with open("/proc/net/dev") as fh:
        for line in fh:
            name, _, rest = line.partition(":")
            if name.strip() != "lo":
                continue
            fields = rest.split()
            return int(fields[0]), int(fields[8])   # rx, tx
    return None


def _loopback_macos():
    """``netstat -ib`` 的 lo0 行。

    列含义随系统版本略有差异，但前几列固定：
    Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes
    所以收字节在第 7 列（下标 6），发字节在第 10 列（下标 9）。
    """
    out = subprocess.run(["netstat", "-ib"], capture_output=True,
                         text=True, timeout=5).stdout
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 10 and fields[0].startswith("lo"):
            return int(fields[6]), int(fields[9])
    return None
