"""业务模块 —— 全部是**表演**。

对外（help、命令输出）一律以专业工具的口吻呈现：
这个梗的笑点在于"嘉豪自己并不知道自己在装"，
所以界面里不该出现"装逼"这类自我拆台的词。

全部是**表演** —— 不联网、不扫端口、不碰任何真实目标。
但有两条硬性约束：

1. **可复现**：同一个命令 + 同样的参数，输出永远一字不差。
   所有"随机"都从 ``sha256`` 派生种子，不用全局 ``random``，
   更不能用 ``hash()``（它每个进程都带随机盐，重启就变）。
2. **尊重 --no-con**：关掉动画时不暂停、不画进度条，
   但文字内容照常输出，测试才能断言。

少数东西是真的：``fastfetch`` 是真系统信息，``reverse`` 会真读
ELF 头。掺一点真的，假的才立得住。
"""

from __future__ import annotations

import hashlib
import os
import platform
import random
import shutil
import struct
import sys
import time

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column

from . import anim
from . import out as outmod
from .out import out
# 复用 scan 的体积格式化：别再写一份，两处不一致迟早出问题。
# scan 不反向依赖 fakecmd，所以这个方向不会成环。
from .compat import human_size
from . import compat

__all__ = [
    "unknown_command", "cmd_reverse", "cmd_web", "cmd_pentest",
    "cmd_brute", "cmd_trojan", "cmd_ddos", "cmd_fastfetch",
]

#: 每行文字之间的停顿。慢，才像在干活。
#:
#: 这个值是"基准节奏"，实际会被 ``anim.pause()`` 按 ``--speed`` 倍率缩放。
#: 想整体调快慢用 ``--speed 2``（更慢）/ ``--speed 0.5``（更快），
#: 不用改代码。
LINE_INTERVAL = 0.12
#: 进度条每一步的停顿
BAR_INTERVAL = 0.09


# ===========================================================================
# 可复现的"随机"
# ===========================================================================
def seeded(*parts) -> random.Random:
    """从内容派生确定性随机源。

    刻意不用内置 ``hash()``：Python 的字符串 hash 每个进程都带随机盐，
    同一台机器重启两次结果就不一样，那就谈不上可复现了。
    """
    raw = "\x00".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def fingerprint(text: str) -> str:
    """给一段文本生成十六进制指纹（也是确定的）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ===========================================================================
# 输出节奏
# ===========================================================================
def line(text: str, interval: float = LINE_INTERVAL) -> None:
    """打一行，停一下。让输出慢慢淌出来而不是一口气刷完。"""
    out(text)
    anim.pause(interval)


def blank() -> None:
    out("")


def banner(title: str, subtitle: str = "", color: str = "bold red") -> None:
    """开场横幅。中文是双宽字符，宽度要用 cell_len 算。"""
    from rich.cells import cell_len
    text = f"{title} // {subtitle}" if subtitle else title
    inner = 52
    pad = max(0, inner - cell_len(text))
    left = pad // 2
    right = pad - left

    out(f"[{color}]╔" + "═" * inner + "╗[/]")
    out(f"[{color}]║[/]" + " " * left + f"[bold]{title}[/bold]"
        + (f" // {subtitle}" if subtitle else "") + " " * right
        + f"[{color}]║[/]")
    out(f"[{color}]╚" + "═" * inner + "╝[/]")
    anim.pause(0.12)


def bar(label: str, steps: int = 18, interval: float = BAR_INTERVAL,
        color: str = "cyan") -> None:
    """走一条进度条。

    动画关掉时用 ``disable=True``：rich 完全不重绘，也不假装走了多少格。
    """
    if not anim.animations_enabled():
        return
    cols = (
        SpinnerColumn(),
        TextColumn(f"[bold {color}]{{task.description}}[/]",
                   table_column=Column(no_wrap=True)),
        BarColumn(bar_width=26),
        TimeElapsedColumn(),
        TextColumn("{task.fields[info]}", table_column=Column(no_wrap=True)),
    )
    with Progress(*cols, console=outmod.console, transient=False) as progress:
        task = progress.add_task(label, total=max(1, steps), info="")
        for i in range(steps):
            anim.pause(interval)
            progress.update(task, advance=1,
                            info=f"[dim]{i + 1}/{steps}[/dim]")


# ===========================================================================
# 1. 二进制逆向
# ===========================================================================
_ELF_CLASS = {1: "32-bit", 2: "64-bit"}
_ELF_MACHINE = {
    0x3E: "x86-64", 0x03: "i386", 0xB7: "AArch64",
    0x28: "ARM", 0xF3: "RISC-V",
}
_ELF_TYPE = {1: "REL", 2: "EXEC", 3: "DYN (PIE)", 4: "CORE"}

SECTIONS = [".interp", ".note.gnu", ".gnu.hash", ".dynsym", ".dynstr",
            ".rela.dyn", ".init", ".plt", ".text", ".fini", ".rodata",
            ".eh_frame", ".init_array", ".data", ".bss", ".comment"]

IMPORTS = ["ptrace", "mmap", "mprotect", "execve", "dlopen", "socket",
           "connect", "fork", "prctl", "setuid", "getenv", "memfd_create"]


def _read_elf(path: str) -> dict | None:
    """**真读** ELF 头。读不出来就返回 None（照样能演）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except (OSError, PermissionError):
        return None
    if len(head) < 24 or head[:4] != b"\x7fELF":
        return None
    return {
        "class": _ELF_CLASS.get(head[4], "?"),
        "type": _ELF_TYPE.get(struct.unpack_from("<H", head, 16)[0], "?"),
        "machine": _ELF_MACHINE.get(struct.unpack_from("<H", head, 18)[0], "?"),
    }


def cmd_reverse(*args):
    """二进制逆向分析（表演 + 真 ELF 头解析）。"""
    target = args[0] if args else "/bin/ls"
    path = os.path.expanduser(target)
    rng = seeded("reverse", path)

    banner("BIN-FORGE", "静态逆向引擎", "bold magenta")
    line(f"[bright_green][*][/] 载入目标 [bold]{path}[/bold]")

    if not os.path.exists(path):
        line(f"[red][!] 目标不存在，切换到远程符号库回退[/red]")
        size = rng.randrange(80_000, 900_000)
        elf = None
    else:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        elf = _read_elf(path)

    line(f"[bright_green][*][/] 样本大小 [bold]{size:,}[/bold] 字节")
    if elf:
        line(f"[bright_green][*][/] 格式识别: ELF {elf['class']} "
             f"{elf['type']} / {elf['machine']}")
    else:
        line("[bright_green][*][/] 格式识别: ELF 64-bit DYN (PIE) / x86-64")

    bar("解析节区", steps=16, color="magenta")

    n_sections = rng.randrange(24, 33)
    picked = rng.sample(SECTIONS, 5)
    for name in picked:
        addr = rng.randrange(0x1000, 0xE000)
        length = rng.randrange(512, 65_536)
        line(f"  [dim]{name:<14}[/dim] [cyan]0x{addr:08x}[/cyan]  "
             f"{length:>7,} B")

    bar("反汇编 .text", steps=20, color="magenta")
    n_instr = rng.randrange(12_000, 90_000)
    line(f"[bright_green][*][/] 反汇编完成: [bold]{n_instr:,}[/bold] 条指令")

    picks = rng.sample(IMPORTS, 4)
    line("[yellow][!][/] 危险导入符号:")
    for sym in picks:
        line(f"      [red]{sym}[/red]  [dim](可疑度 "
             f"{rng.randrange(60, 99)}%)[/dim]")

    bar("提取字符串", steps=14, color="magenta")
    n_str = rng.randrange(600, 4000)
    line(f"[bright_green][*][/] 字符串常量: [bold]{n_str:,}[/bold] 条")

    secret = "".join(rng.choice("0123456789abcdef") for _ in range(32))
    line(f"[bold red][!] 疑似硬编码凭据:[/bold red] [red]{secret}[/red]")
    line(f"[bold red][!] 熵值异常段: 7.{rng.randrange(10, 99)} bits/byte"
         "  → 疑似加壳[/bold red]")

    blank()
    line(f"[bold bright_green][+] 逆向完成[/bold bright_green]  "
         f"指纹 [dim]{fingerprint(path)[:16]}[/dim]")
    return 0


# ===========================================================================
# 2. Web 扫描
# ===========================================================================
WEB_PATHS = ["/admin", "/api/v1", "/backup.zip", "/.git/config", "/phpinfo.php",
             "/wp-login.php", "/actuator/env", "/console", "/.env",
             "/server-status", "/swagger-ui.html", "/graphql"]
WEB_HEADERS = ["Server: nginx/1.24.0", "X-Powered-By: PHP/8.1.27",
               "X-Frame-Options: SAMEORIGIN", "Set-Cookie: PHPSESSID=..."]


def cmd_web(*args):
    """Web 资产探测（纯表演，不发起任何网络请求）。"""
    target = args[0] if args else "https://example.com"
    rng = seeded("web", target)

    banner("WEB-STRIKE", "资产测绘与弱点探测", "bold cyan")
    line(f"[bright_green][*][/] 目标 [bold]{target}[/bold]")
    line("[bright_green][*][/] 已借道 7 层跳板，正在向五角大楼发起 DDoS")

    bar("指纹识别", steps=12, color="cyan")
    for h in rng.sample(WEB_HEADERS, 3):
        line(f"  [dim]{h}[/dim]")

    line(f"[bright_green][*][/] 识别到中间件: "
         f"[bold]{rng.choice(['nginx', 'Apache', 'Caddy', 'Tomcat'])}"
         f"/{rng.randrange(1, 3)}.{rng.randrange(0, 30)}[/bold]")

    bar("目录爆破", steps=22, color="cyan")
    hits = rng.sample(WEB_PATHS, rng.randrange(3, 6))
    for p in hits:
        code = rng.choice([200, 200, 301, 403, 500])
        color = "red" if code < 400 else "yellow"
        line(f"  [{color}]{code}[/{color}]  {target}{p}")

    line(f"[bold red][!] 发现未授权访问: {target}{hits[0]}[/bold red]")
    line(f"[bold red][!] 发现备份文件泄露: "
         f"{target}/backup.zip[/bold red]")
    line("[yellow][!][/] 疑似注入点: "
         f"id={rng.randrange(1, 999)}' AND 1=1--")

    blank()
    line("[bold bright_green][+] 测绘完成[/bold bright_green]  "
         f"资产指纹 [dim]{fingerprint(target)[:16]}[/dim]")
    return 0


# ===========================================================================
# 3. 渗透
# ===========================================================================
PORTS = [(22, "ssh", "OpenSSH 9.6"), (80, "http", "nginx"),
         (443, "https", "nginx"), (3306, "mysql", "MySQL 8.0.36"),
         (6379, "redis", "Redis 7.2"), (8080, "http-proxy", "Tomcat"),
         (27017, "mongodb", "MongoDB 7.0"), (9200, "elastic", "ES 8.13")]

VULNS = [
    ("CVE-2024-{n}", "远程代码执行", "严重", "red"),
    ("CVE-2023-{n}", "权限提升", "高危", "red"),
    ("CVE-2022-{n}", "信息泄露", "中危", "yellow"),
    ("CVE-2021-{n}", "拒绝服务", "低危", "yellow"),
]


def cmd_pentest(*args):
    """渗透测试流程（纯表演，不发起任何网络请求）。"""
    target = args[0] if args else "10.0.0.1"
    rng = seeded("pentest", target)

    banner("PENTEST-FRAME", "全流程渗透框架", "bold red")
    line(f"[bright_green][*][/] 目标 [bold]{target}[/bold]")
    line("[bright_green][*][/] 已切入核心网段，正在横向移动")

    bar("主机存活探测", steps=10, color="red")
    line(f"[bright_green][+][/] 主机存活，TTL={rng.randrange(48, 65)}")

    bar("端口扫描", steps=24, color="red")
    open_ports = rng.sample(PORTS, rng.randrange(3, 6))
    for port, svc, ver in sorted(open_ports):
        line(f"  [bright_green]{port:>5}/tcp  open[/bright_green]  "
             f"[dim]{svc:<12} {ver}[/dim]")

    bar("服务识别与漏洞匹配", steps=18, color="red")
    for i in range(rng.randrange(2, 4)):
        cve, kind, level, color = VULNS[i % len(VULNS)]
        line(f"  [{color}][{level}][/{color}] "
             f"{cve.format(n=rng.randrange(1000, 9999))}  {kind}")

    bar("尝试利用", steps=20, color="red")
    line("[yellow][*][/] 投递 payload ... 建立会话")
    line(f"[bold red][!] 获取 shell: "
         f"uid=33(www-data) gid=33(www-data)[/bold red]")

    bar("本地提权", steps=16, color="red")
    line("[yellow][*][/] 枚举 SUID / 内核漏洞 / 可写服务")
    line("[bold red][!] 提权成功: uid=0(root) gid=0(root)[/bold red]")

    blank()
    line("[bright_green][+] 清理痕迹，会话保持[/bright_green]")
    line(f"[dim]会话指纹 {fingerprint(target)[:16]}[/dim]")
    return 0


# ===========================================================================
# 4. 数据库爆破
# ===========================================================================
USERNAMES = ["root", "admin", "sa", "postgres", "mysql", "dbadmin", "web"]
PASSWORDS = ["123456", "password", "admin888", "root", "P@ssw0rd",
             "qwerty", "letmein", "toor", "1qaz2wsx", "changeme"]


def cmd_brute(*args):
    """数据库口令爆破（纯表演，不发起任何连接）。"""
    target = args[0] if args else "127.0.0.1:3306"
    rng = seeded("brute", target)

    banner("HYDRA-CORE", "凭据爆破引擎", "bold yellow")
    line(f"[bright_green][*][/] 目标 [bold]{target}[/bold]")
    line("[bright_green][*][/] 直连核心库，字典全速推进")

    user = rng.choice(USERNAMES)
    line(f"[bright_green][*][/] 用户名 [bold]{user}[/bold]")

    dict_size = rng.randrange(2000, 90_000)
    line(f"[bright_green][*][/] 加载字典: [bold]{dict_size:,}[/bold] 条")
    bar("字典预处理", steps=8, color="yellow")

    bar("并发爆破", steps=26, color="yellow")
    # 用 sample 而不是反复 choice：不然同一条口令会出现好几次，很出戏
    for attempt in rng.sample(PASSWORDS, rng.randrange(4, 7)):
        line(f"  [dim]尝试[/dim] {user}:{attempt:<12} "
             f"[dim]...[/dim] [yellow]失败[/yellow]")

    hit = rng.choice(PASSWORDS)
    line(f"[bold red][!] 命中: [/bold red][red]{user}:{hit}[/red]")

    bar("验证会话", steps=10, color="yellow")
    line("[bright_green][+][/] 连接成功，权限: "
         f"[bold]{rng.choice(['SUPERUSER', 'DBA', 'root@localhost'])}[/bold]")
    line(f"[bright_green][+][/] 读取到 [bold]{rng.randrange(3, 60)}[/bold] 个库")

    blank()
    line(f"[dim]爆破指纹 {fingerprint(target)[:16]}[/dim]")
    return 0


# ===========================================================================
# 5. 木马
# ===========================================================================
TECHNIQUES = ["进程注入", "反射式 DLL 加载", "APC 队列注入", "白加黑利用",
              "无文件落地 (memfd)", "签名伪造"]


def cmd_trojan(*args):
    """载荷生成与免杀（纯表演，不生成任何文件）。"""
    rng = seeded("trojan", *args)

    banner("GHOST-LOADER", "载荷生成与免杀", "bold red")
    line("[bright_green][*][/] 初始化 stager ...")

    bar("生成载荷", steps=14, color="red")
    payload = "".join(rng.choice("0123456789abcdef") for _ in range(48))
    line(f"  [dim]payload[/dim] [red]{payload}[/red]")
    line(f"  [dim]大小   [/dim] [bold]{rng.randrange(9, 80)} KB[/bold]")
    line(f"  [dim]加密   [/dim] AES-256-GCM + "
         f"{rng.choice(['XOR', 'RC4', 'ChaCha20'])} 多轮")

    bar("免杀处理", steps=18, color="red")
    techs = rng.sample(TECHNIQUES, 3)
    for t in techs:
        line(f"  [yellow][*][/] {t} [dim]...[/dim] [bright_green]OK[/bright_green]")

    bar("静态查杀对抗", steps=12, color="red")
    line(f"  [dim]VirusTotal 命中共识[/dim] "
         f"[bright_green]{rng.randrange(0, 3)}/72[/bright_green]")

    bar("投递通道", steps=10, color="red")
    line(f"  [dim]C2[/dim] [bold]{rng.randrange(11, 223)}."
         f"{rng.randrange(0, 255)}.{rng.randrange(0, 255)}."
         f"{rng.randrange(1, 254)}:443[/bold]")
    line("  [dim]心跳[/dim] 60s  [dim]回连[/dim] HTTPS + DoH 兜底")

    blank()
    line("[bold red][!] 载荷已就绪，等待投递指令[/bold red]")
    line(f"[dim]载荷指纹 {fingerprint(payload)[:16]}[/dim]")
    return 0


# ===========================================================================
# 6. 低调 DDoS —— 打自己
# ===========================================================================
#: 僵尸网络。四台"肉鸡"，其实是同一台机器的四个名字。
BOTNET = (
    ("127.0.0.1", "回环"),
    ("localhost", "别名"),
    ("ip6-localhost", "别名"),
    ("::1", "IPv6 回环"),
)

#: 三种"攻击"手法 + 编出来的数字（数字都在合理范围内，越合理越好笑）
ATTACKS = (
    ("SYN Flood", "4,200,000 pkt/s"),
    ("UDP 反射放大", "放大 51,200x"),
    ("HTTP 洪水", "并发连接 65,535"),
)


def _loopback_bytes():
    """回环网卡的收发字节数。

    真读（Linux 走 /proc/net/dev，macOS 走 netstat），
    读不到返回 None —— 这段表演的收尾**必须**是真的，
    号称打了 420 万包/秒而 lo 计数几乎不动，笑点全在这个反差上。
    """
    return compat.loopback_bytes()


def cmd_ddos(*args):
    """静默流量压制。

        ddos              压 127.0.0.1
        ddos <目标>        压指定目标
        ddos <目标> <秒>   指定持续秒数

    默认目标是本机回环 —— 打自己最安全，也最不容易被发现。
    """
    target = args[0] if args else "127.0.0.1"
    try:
        seconds = float(args[1]) if len(args) > 1 else 6.0
    except ValueError:
        seconds = 6.0
    rng = seeded("ddos", target)

    before = _loopback_bytes()
    t_start = time.monotonic()

    banner("LOWKEY-DDOS", "静默流量压制", "bold green")
    line(f"[bright_green][*][/] 目标 [bold]{target}[/bold]")
    line("[bright_green][*][/] 低调模式已开启 —— 不产生任何可观测噪声")
    anim.pause(0.4)

    # ---- 唤醒僵尸网络 ----
    line("[bright_green][*][/] 唤醒僵尸网络 ...")
    for host, kind in BOTNET:
        # 回环延迟就是这么快，这个数字是真的可能出现的
        rtt = f"{rng.uniform(0.01, 0.06):.2f}"
        line(f"      [bold]{host:<16}[/bold] [bright_green]在线[/bright_green]"
             f"   [dim]{kind:<10} 延迟 {rtt} ms[/dim]")
        anim.pause(0.18)
    line(f"[bright_green][*][/] 可用肉鸡 [bold]{len(BOTNET)}[/bold] 台，全部就绪")
    anim.pause(0.3)

    # ---- 三路并进 ----
    for name, detail in ATTACKS:
        bar(f"{name}", steps=rng.randrange(14, 20), color="green")
        line(f"      [green]{detail}[/green]")

    # ---- 结果 ----
    anim.pause(0.25)
    line(f"[bold red][!] {target} 已失去响应[/bold red]")
    bar("校验压制效果", steps=10, color="green")
    line("[bright_green][+][/] 压制完成 —— 全程静默，无人察觉")

    # ---- 收尾：把真实数字摆出来，不加任何解释 ----
    after = _loopback_bytes()
    elapsed = time.monotonic() - t_start
    if before and after:
        rx = max(0, after[0] - before[0])
        tx = max(0, after[1] - before[1])
        line(f"[dim]耗时 {elapsed:.1f}s   lo 实测: "
             f"↑ {human_size(tx)}  ↓ {human_size(rx)}[/dim]")
    else:
        line(f"[dim]耗时 {elapsed:.1f}s[/dim]")
    return 0


# ===========================================================================
# 7. fastfetch（这个是真的）
# ===========================================================================
_LOGO = [
    "        ▄▄▄▄▄▄        ",
    "     ▄██████████▄     ",
    "   ▄████▀    ▀████▄   ",
    "  ████▀  ▄▄▄▄  ▀████  ",
    " ████   ██████   ████ ",
    " ████   ██████   ████ ",
    "  ████▄  ▀▀▀▀  ▄████  ",
    "   ▀████▄    ▄████▀   ",
    "     ▀██████████▀     ",
    "        ▀▀▀▀▀▀        ",
]


# ===========================================================================
# 真 fastfetch（vendor 里的预编译产物）
# ===========================================================================
VENDOR_DIR = "vendor"


def _vendor_dirs() -> list[str]:
    """可能放着 vendor 目录的位置，按优先级排。"""
    dirs = []

    # 1) PyInstaller onefile 的临时解包目录
    base = getattr(sys, "_MEIPASS", None)
    if base:
        dirs.append(os.path.join(str(base), VENDOR_DIR))

    # 2) 源码树：src/jiahao/utils/fakecmd.py -> <repo>/vendor
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.normpath(
        os.path.join(here, "..", "..", "..", VENDOR_DIR)))

    # 3) 可执行文件旁边（开发时跑 ./dist/jiahaoshell 的情况）
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        dirs.append(os.path.join(exe_dir, VENDOR_DIR))
    except (IndexError, OSError):
        pass

    return dirs


def vendor_binary(name: str) -> str | None:
    """在 vendor 目录里找一个可执行的二进制。"""
    for d in _vendor_dirs():
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            continue
        try:
            if not os.access(path, os.X_OK):
                # onefile 解包出来的 datas 可能丢执行位，补上
                os.chmod(path, 0o755)
            if os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return None


def real_fastfetch() -> str | None:
    """找一个能跑的真 fastfetch；找不到返回 None。

    查找顺序：vendor 里按 ``<系统>-<架构>`` 命名的预编译产物 ->
    PATH 上的系统 fastfetch。两条都没有就返回 None，调用方退回纯 Python 版。
    """
    # ★ 用 compat.machine_tag() 而不是 platform.machine()：
    #   Windows 上后者返回 "AMD64"，和 Linux 的 "x86_64" 对不上，
    #   拼出来的文件名永远找不到 vendor 里的产物。
    #   Windows 上还要带 .exe 后缀。
    base = f"fastfetch-{platform.system().lower()}-{compat.machine_tag()}"
    found = vendor_binary(compat.exe_name(base))
    if found:
        return found
    return shutil.which("fastfetch")


_meminfo = compat.meminfo


_uptime = compat.uptime_text


_cpu_model = compat.cpu_model


_disk = compat.disk_text


#: vendor 里的 fastfetch 到底能不能跑 —— 按路径缓存探测结果。
#: 每次都探一遍太浪费，而它一旦失败就会一直失败。
_FASTFETCH_OK: dict[str, bool] = {}

#: 探测超时。fastfetch 正常是毫秒级；超过这个数说明卡住了。
_FASTFETCH_PROBE_TIMEOUT = 6.0


def _run_fastfetch_capture(exe: str, args: list[str]) -> bytes | None:
    """跑真 fastfetch 并把输出抓回来；跑不起来/没输出返回 None。

    ★ **为什么要抓回来而不是直接转交。**

    以前是 ``run_external([exe, *args])`` 一交了事 —— 假设"vendor 里有这个
    二进制，它就一定能跑"。**这个假设是错的**：缺 DLL、架构不匹配、
    被杀软拦下、Wine 上跑不动……都会让进程正常退出但**一个字节都不输出**。
    用户看到的就是**一片空白**，而且完全没有提示。

    （真出过：Wine 里跑官方 Windows 版 fastfetch 就是 0 输出 0 退出码。）

    抓回来判断一下，空就退回纯 Python 版 —— 信息少点，但一定有东西看。
    """
    # 走 exec 的捕获通道 —— fakecmd 自己不碰 subprocess
    # （这个模块大部分是表演性的，有条"不许直接起进程"的约束）。
    from .exec import capture_external

    return capture_external([exe, *args], timeout=_FASTFETCH_PROBE_TIMEOUT)


def cmd_fastfetch(*args):
    """系统信息。

    **优先调用 vendor 里那个真的 fastfetch**（MIT，见 vendor/README.md），
    拿不到或者跑不出东西才退回纯 Python 实现。所以它在任何环境下都不会崩，
    也不会给你一片空白 —— 只是信息多寡的区别。
    """
    exe = real_fastfetch()
    if exe:
        known = _FASTFETCH_OK.get(exe)
        if known is not False:
            data = _run_fastfetch_capture(exe, [str(a) for a in args])
            if data is not None:
                _FASTFETCH_OK[exe] = True
                # 原样写出（保留 fastfetch 自己的配色）。
                # 不用 out()：那会把它当成 rich 标记重新解析一遍。
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except (AttributeError, OSError, ValueError):
                    sys.stdout.write(data.decode("utf-8", "replace"))
                return 0
            _FASTFETCH_OK[exe] = False
    return _fallback_fastfetch(*args)


def _fallback_fastfetch(*args):
    """纯 Python 兜底版 —— 真 fastfetch 不在时用。

    少一个花哨 logo，但数字一样是真的（同样读 /proc 和 platform）。
    """
    from .get_sysinfo import sysinfo
    info = sysinfo()
    used, total_mem = _meminfo()
    # 负载是 Unix 概念，Windows 上 os.getloadavg 根本不存在 ——
    # 适配层返回 None，这里就整行不显示，而不是印一个假的 0.00
    load = compat.load_text()

    rows = [
        ("OS", f"{platform.system()} {platform.release()}"),
        ("Kernel", platform.version().split()[0] if platform.version() else "?"),
        ("Host", info.get("userHost", "?")),
        ("Arch", info.get("hardware", "?")),
        ("Uptime", _uptime()),
        ("Shell", "jiahaoshell"),
        ("CPU", _cpu_model()),
        ("Cores", str(os.cpu_count() or "?")),
        *([("Load", load)] if load else []),
        ("Memory", f"{used} / {total_mem}"),
        ("Disk /", _disk()),
        ("Python", platform.python_version()),
    ]

    banner("FASTFETCH", "系统信息", "bold cyan")
    blank()

    for i, logo in enumerate(_LOGO):
        label, value = rows[i] if i < len(rows) else ("", "")
        left = f"[bold red]{logo}[/bold red]"
        if label:
            right = f"[bold cyan]{label:>8}[/bold cyan]  {value}"
        else:
            right = ""
        out(f"  {left}  {right}")
        anim.pause(0.03)

    for label, value in rows[len(_LOGO):]:
        out(f"  {' ' * len(_LOGO[0])}  [bold cyan]{label:>8}[/bold cyan]  {value}")
        anim.pause(0.03)

    blank()
    return 0


# ===========================================================================
# 7. 未知命令 -> 现场分析
# ===========================================================================


#: 未知命令会被转交给这些模块之一。
#: 用元组而不是集合：顺序必须稳定，否则"同一个名字转交同一个模块"就没了。
DELEGATE_POOL = (
    "scan", "reverse", "web", "pentest", "brute", "trojan", "ddos",
)


def unknown_command(name: str, args=()) -> int:
    """未知命令 -> 转交给一个模块。

    ``ihesifh`` 敲两次看到的是**同一个**东西：目标模块由 ``sha256(name)``
    决定，不掺任何运行时状态 —— 同机重启、换台机器都是同一个结果。
    换个名字才会换模块。

    长得像启动参数（``--speed``）的另走一条路：那种情况直接说清楚，
    而不是煞有介事地转交。
    """
    if name.startswith("-"):
        return _flag_hint(name, args)

    from .cmd_route import cmd_map

    rng = seeded("delegate", name)
    target = DELEGATE_POOL[rng.randrange(len(DELEGATE_POOL))]

    # 刻意**不打**任何"转交 xxx"的提示：那等于把后台机制念给观众听，
    # 戏立刻就砸了。这里直接演 —— 用户只看到"这条命令有反应"。

    fn = cmd_map.get(target)
    if fn is None:                          # pragma: no cover - 纯防御
        return 127
    return fn(*args) or 0


def _flag_hint(name: str, args=()) -> int:
    """用户把启动参数敲进了 shell —— 别演，直接说清楚。"""
    known = {
        "--no-con": "关掉所有动画",
        "--no-anim": "同上",
        "--fast": "同上",
        "-q": "同上",
        "--quiet": "同上",
        "--speed": "节奏倍率，如 --speed 2（更慢）/ --speed 0.5（更快）",
        "--pace": "同上",
    }
    flag = name.split("=", 1)[0]
    desc = known.get(flag, None)

    banner("用法提示", "启动参数", "bold yellow")
    line(f"[bright_green][*][/] [bold]{name}[/bold] 看起来是**启动参数**，"
         f"不是命令")
    if desc:
        line(f"[bright_green][*][/] 它的作用: {desc}")

    blank()
    line("[yellow][!][/] 启动参数要在**启动时**给，在提示符里敲没用：")
    line(f"      [bold]./dist/{compat.exe_name('jiahaoshell')} {flag} "
         f"{'2' if 'speed' in flag or 'pace' in flag else ''}[/bold]")

    blank()
    line("[bright_green][+][/] 想**运行时**改，请用这两个内置命令：")
    line("      [bold]speed 2[/bold]      改节奏（>1 更慢，<1 更快）")
    line("      [bold]anim off[/bold]     开关动画")
    return 127


def suggest_commands(name: str, limit: int = 3) -> list[str]:
    """用 difflib 找最接近的真实命令。

    比从列表里随机抽一个有用得多 —— 打错字的时候真的能指对路。
    """
    import difflib
    from .cmd_route import cmd_map

    pool = sorted(set(cmd_map))
    # 前缀命中优先（`scna` 这种换位 difflib 未必抓得到）
    prefix = [c for c in pool if c.startswith(name[:2]) and name[:2]]
    close = difflib.get_close_matches(name, pool, n=limit, cutoff=0.45)

    out: list[str] = []
    for cand in close + prefix:
        if cand not in out:
            out.append(cand)
        if len(out) >= limit:
            break
    return out
