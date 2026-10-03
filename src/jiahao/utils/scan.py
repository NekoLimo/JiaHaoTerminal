"""假装黑客的磁盘扫描 —— 但数字都是真的。

**真的部分**：真遍历文件系统、真统计字节数、真算香农熵、真查权限异常
（全局可写、SUID/SGID、读不了的）。所以报告里每个数字都有出处。

**表演的部分**：分阶段进度条、飞滚的路径、间歇插入的黑客台词、
最后的"可疑目标"告警。

这样它既好笑又真的有点用 —— 比如 `scan /` 能告诉你哪些文件全局可写。
"""

from __future__ import annotations

import math
import os
import stat as statmod
import time
from collections import Counter
from dataclasses import dataclass, field

from rich.cells import cell_len
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column

# 用模块引用而不是 from .out import console —— 这样测试只需要
# patch outmod.console 一处，Progress 和 out() 就会走同一个 buffer
from . import out as outmod
from .out import out
from . import anim
from . import compat

__all__ = ["run_scan", "shannon_entropy", "human_size"]


# ===========================================================================
# 参数
# ===========================================================================
#: 默认时间预算（秒）。扫盘不该让用户干等，超了就报"部分结果"。
DEFAULT_SECONDS = 8.0
#: 默认最多看多少个文件
DEFAULT_MAX_FILES = 200_000
#: 这些是内核/设备伪文件系统，扫了没意义还可能卡住
SKIP_DIRS = frozenset({"/proc", "/sys", "/dev", "/run"})
#: 熵值采样的文件大小范围
ENTROPY_MIN_BYTES = 512
ENTROPY_MAX_BYTES = 64 * 1024 * 1024
#: 最多采样多少个文件算熵（真读磁盘，必须限量）
ENTROPY_SAMPLES = 24
#: 保留多少个"最大文件"
TOP_N = 8
#: 遍历时最多流式打印多少行路径。再多就是刷屏，反而看不清。
STREAM_LINES = 70
#: 流式输出每行之间停多久 —— 慢，才像在干活
STREAM_INTERVAL = 0.06

#: 间歇插入的台词。纯表演，但配合真进度条效果很好。
FLAVOR = [
    "[*] 初始化 DISK-REAVER 引擎 v4.2.1",
    "[*] 加载扇区映射表 ................. OK",
    "[*] 校验 RAID 校验和 ............... OK",
    "[*] 绕过 SMART 健康检查",
    "[*] 重建 inode 索引树",
    "[*] 解密文件分配表 (FAT/MFT)",
    "[*] 嗅探残留扇区数据",
    "[*] 关联已删除记录与 inode",
    "[*] 提取隐藏分区签名",
    "[*] 比对已知取证工具指纹",
]

#: 熵值高于这个就当"疑似加密/压缩"
HIGH_ENTROPY = 7.5


#: 字节 -> 人类可读。实现放在 compat 里，全项目共用一份，别处不再复制。
human_size = compat.human_size


def shannon_entropy(data: bytes) -> float:
    """香农熵（bits/byte）。

    0 表示完全可预测（全零、纯文本很低），8 表示完全随机
    （加密数据、已压缩数据）。这是真的算，不是编的。
    """
    if not data:
        return 0.0
    n = len(data)
    counts = Counter(data)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# ===========================================================================
# 结果
# ===========================================================================
@dataclass
class Finding:
    kind: str          # 权限过宽 / SUID / 读不了 / 高熵
    path: str
    detail: str


@dataclass
class ScanResult:
    root: str
    files: int = 0
    dirs: int = 0
    total_bytes: int = 0
    errors: int = 0
    truncated: bool = False
    elapsed: float = 0.0
    biggest: list[tuple[str, int]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    entropy_hits: list[tuple[str, float]] = field(default_factory=list)


# ===========================================================================
# 遍历
# ===========================================================================
#: 这个平台支不支持 SUID/SGID 位（Windows 不支持）
_SUID_SUPPORTED = compat.supports_suid()


def _check_perms(path: str, st: os.stat_result, findings: list[Finding],
                 root: str) -> None:
    """权限审计 —— 只报**真正值得看**的东西。

    刻意不报"组可写"：常见 umask（002）下文件默认就是 0o664 组可写，
    报出来几乎每个文件都中，会把真正的信号（全局可写 / SUID）淹没。
    """
    mode = st.st_mode

    # Windows 是 ACL 模型，没有 SUID/SGID 这三个位。硬查的结果永远是"没找到"，
    # 报出来就是**误导**（"未发现权限异常"听着像查过了，其实根本没这个概念）。
    if _SUID_SUPPORTED:
        if mode & statmod.S_ISUID:
            findings.append(Finding("SUID", path, "可提权执行"))
        elif mode & statmod.S_ISGID:
            findings.append(Finding("SGID", path, "继承组权限"))

    if mode & statmod.S_IWOTH:
        findings.append(Finding("全局可写", path, oct(mode & 0o777)))


def _fit(text: str, limit: int) -> str:
    """把路径截到 ``limit`` 个字符，中间用 … 省略。"""
    if limit < 8:
        limit = 8
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = keep // 3
    return text[:head] + "…" + text[-(keep - head):]


def _path_budget() -> int:
    """当前终端宽度下，路径列还能放多少字符。"""
    try:
        width = outmod.console.width
    except Exception:                       # noqa: BLE001 - 拿不到就给个保守值
        width = 80
    return max(16, width - 52)


def _walk(root: str, result: ScanResult, max_files: int, deadline: float,
          progress: Progress, task, flavor_every: int = 4000) -> None:
    """真遍历。用 os.scandir 手动压栈，比 os.walk 快一些，
    而且能在循环里检查时间预算。"""
    stack = [root]
    seen = 0
    flavor_i = 0
    streamed = 0
    budget = _path_budget()
    # 动画关掉时不吐流式路径：测试环境要的是干净、可断言的输出
    stream = anim.animations_enabled()

    while stack:
        if result.files >= max_files or time.monotonic() > deadline:
            result.truncated = True
            break

        current = stack.pop()
        try:
            entries = list(os.scandir(current))
            result.dirs += 1
        except (PermissionError, OSError):
            result.errors += 1
            continue

        for entry in entries:
            if result.files >= max_files or time.monotonic() > deadline:
                result.truncated = True
                stack.clear()
                break

            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.path not in SKIP_DIRS:
                        stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
            except (PermissionError, OSError):
                result.errors += 1
                continue

            result.files += 1
            result.total_bytes += st.st_size
            seen += 1

            # 最大的 N 个
            if st.st_size:
                result.biggest.append((entry.path, st.st_size))
                if len(result.biggest) > TOP_N * 4:
                    result.biggest.sort(key=lambda kv: kv[1], reverse=True)
                    del result.biggest[TOP_N:]

            _check_perms(entry.path, st, result.findings, root)

            # ★ 流式吐路径：一行一行慢慢出来，而不是最后一次性甩报告。
            # 进度条由 Progress 自己维护，路径走 console.print 打在它上方。
            if stream and streamed < STREAM_LINES:
                progress.console.print(
                    f"  [dim]├─[/dim] [bright_black]{_fit(entry.path, budget)}"
                    f"[/bright_black]"
                )
                streamed += 1
                anim.pause(STREAM_INTERVAL)

            # 间歇插台词（真进度条 + 假台词）
            if flavor_every and seen % flavor_every == 0:
                progress.console.print(
                    f"[bright_green]{FLAVOR[flavor_i % len(FLAVOR)]}[/bright_green]"
                )
                flavor_i += 1

    result.biggest.sort(key=lambda kv: kv[1], reverse=True)
    del result.biggest[TOP_N:]


def _sample_entropy(result: ScanResult, deadline: float,
                    progress: Progress, task) -> None:
    """对一批文件真算香农熵，找出疑似加密/压缩的。"""
    candidates = [
        p for p, size in result.biggest
        if ENTROPY_MIN_BYTES <= size <= ENTROPY_MAX_BYTES
    ][:ENTROPY_SAMPLES]

    progress.update(task, total=max(1, len(candidates)))
    for path in candidates:
        if time.monotonic() > deadline:
            break
        try:
            with open(path, "rb") as fh:
                head = fh.read(4096)
                fh.seek(max(0, os.path.getsize(path) // 2))
                head += fh.read(4096)
        except (PermissionError, OSError):
            result.errors += 1
            progress.update(task, advance=1)
            continue

        entropy = shannon_entropy(head)
        if entropy >= HIGH_ENTROPY:
            result.entropy_hits.append((path, entropy))
        progress.update(task, advance=1,
                        stats=f"{entropy:.2f} bits/byte",
                        info=f"[dim]{_fit(path, _path_budget())}[/dim]")


# ===========================================================================
# 呈现
# ===========================================================================
def _mount_info(root: str) -> str:
    """挂载点容量。

    用 ``shutil.disk_usage``（适配层封的）而不是 ``os.statvfs`` ——
    后者 Windows 没有。
    """
    got = compat.disk_bytes(root)
    if got is None:
        return "无法读取挂载信息"
    _used, total, free = got
    used_pct = 100.0 * (total - free) / total if total else 0.0
    return (f"{human_size(total)} 容量 / {human_size(free)} 可用 "
            f"({used_pct:.0f}% 已用)")


def _banner(root: str) -> None:
    # 中文是双宽字符，len() 算出来的宽度是错的，必须用 cell_len。
    # 手写空格padding 会让右边框对不齐。
    title = "DISK-REAVER // 深度扇区扫描引擎"
    inner = 52
    pad = max(0, inner - cell_len(title))
    left = pad // 2
    right = pad - left

    out("[bold red]╔" + "═" * inner + "╗[/bold red]")
    out("[bold red]║[/bold red]"
        + " " * left
        + "[bold red]DISK-REAVER[/bold red] // 深度扇区扫描引擎"
        + " " * right
        + "[bold red]║[/bold red]")
    out("[bold red]╚" + "═" * inner + "╝[/bold red]")
    out(f"[bright_green][*] 目标:[/bright_green] [bold]{root}[/bold]")
    out(f"[bright_green][*] 挂载:[/bright_green] {_mount_info(root)}")
    out("")


def _theatrical_intro(lines: int = 4, delay: float = 0.12) -> None:
    """开场先念几行台词。

    小目录真扫起来只要几毫秒，光秃秃的没气氛；这几行 + 轻微停顿
    能把仪式感补上，代价不到半秒。

    动画关掉时整段跳过（``--no-con`` / ``JIAHAO_NO_ANIM=1``）。
    """
    if not anim.animations_enabled():
        return
    for i in range(max(0, lines)):
        out(f"[bright_green]{FLAVOR[i % len(FLAVOR)]}[/bright_green]")
        anim.pause(delay)


def run_scan(target: str = ".", seconds: float = DEFAULT_SECONDS,
             max_files: int = DEFAULT_MAX_FILES,
             intro: bool = True) -> int:
    """扫一遍 ``target`` 并打一份戏剧化的报告。"""
    root = os.path.abspath(os.path.expanduser(target))
    if not os.path.isdir(root):
        out(f"scan: 不是目录: {target}", style="red")
        return 1

    _banner(root)
    if intro:
        _theatrical_intro()
    out("")

    result = ScanResult(root=root)
    deadline = time.monotonic() + max(0.5, seconds)
    started = time.monotonic()

    # 路径列占了大部分宽度，其余列一律 no_wrap 固定住，
    # 否则长路径会把描述和计数挤成 "遍历 …"
    _nowrap = Column(no_wrap=True)
    bar = (
        SpinnerColumn(),
        TextColumn("[bold]{task.description}", table_column=_nowrap),
        BarColumn(bar_width=20),
        TimeElapsedColumn(),
        TextColumn("{task.fields[stats]}", table_column=_nowrap),
        TextColumn("{task.fields[info]}", table_column=_nowrap),
    )

    # 动画关掉时把进度条也停掉：它每帧重绘，输出既长又难断言。
    # disable=True 让 rich 完全不做实时渲染，只保留最终结果。
    _live = anim.animations_enabled()

    try:
        with Progress(*bar, console=outmod.console, transient=False,
                      disable=not _live) as progress:
            # ---- 阶段 1：遍历（真干活）----
            # total=None -> 不确定进度条。事先确实不知道有多少文件，
            # 硬填一个 max_files 只会让百分比永远停在 0%。
            t1 = progress.add_task("[cyan]遍历索引[/cyan]", total=None,
                                   stats="", info="")
            _walk(root, result, max_files, deadline, progress, t1)
            progress.update(
                t1, total=1, completed=1,
                stats=f"{result.files:,} 文件 {human_size(result.total_bytes)}",
                info="",
            )

            # ---- 阶段 2：熵值分析（真读盘）----
            t2 = progress.add_task("[magenta]熵值分析[/magenta]", total=1,
                                   stats="", info="")
            _sample_entropy(result, deadline, progress, t2)

            # ---- 阶段 3：收尾 ----
            t3 = progress.add_task("[green]生成报告[/green]", total=1,
                                   stats="", info="")
            progress.update(t3, advance=1)

    except KeyboardInterrupt:
        out("\n[bold red][!] 扫描被中断，以下是已收集的部分结果[/bold red]")

    result.elapsed = time.monotonic() - started
    _report(result)
    return 0


def _report(r: ScanResult) -> None:
    out("")
    out("[bold bright_green][+] 扫描完成[/bold bright_green] "
        f"用时 [bold]{r.elapsed:.2f}s[/bold]")

    if r.truncated:
        out("[yellow][!] 达到预算上限，结果不完整（加参数可扫更深）[/yellow]")
    if r.errors:
        out(f"[yellow][i] {r.errors} 个条目无权限访问，已跳过[/yellow]")

    out("")
    out("[bold]── 索引统计 " + "─" * 40 + "[/bold]")
    out(f"  文件  [bold bright_cyan]{r.files:,}[/bold bright_cyan]    "
        f"目录  [bold bright_cyan]{r.dirs:,}[/bold bright_cyan]    "
        f"体积  [bold bright_cyan]{human_size(r.total_bytes)}[/bold bright_cyan]")

    if r.biggest:
        out("")
        out("[bold]── 最大目标 " + "─" * 40 + "[/bold]")
        for path, size in r.biggest[:TOP_N]:
            shown = path if len(path) <= 58 else "…" + path[-57:]
            out(f"  [bright_blue]{human_size(size):>10}[/bright_blue]  {shown}")

    if r.entropy_hits:
        out("")
        out("[bold]── 高熵目标（疑似加密/压缩）" + "─" * 26 + "[/bold]")
        for path, entropy in sorted(r.entropy_hits, key=lambda kv: -kv[1])[:6]:
            shown = path if len(path) <= 52 else "…" + path[-51:]
            out(f"  [bright_magenta]{entropy:.2f} bits/byte[/bright_magenta]  {shown}")

    if not _SUID_SUPPORTED:
        # 说清楚为什么没这一项，而不是让人以为"扫过了，没问题"
        out("")
        out("[bright_black][i] 本平台无 SUID/SGID 概念，该项检查已跳过"
            "[/bright_black]")

    # 权限类发现按类型归并，避免刷屏
    by_kind: dict[str, list[Finding]] = {}
    for f in r.findings:
        by_kind.setdefault(f.kind, []).append(f)

    if by_kind:
        out("")
        out("[bold bright_red]── 可疑目标 " + "─" * 40 + "[/bold bright_red]")
        for kind, items in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
            out(f"  [bold red][!] {kind}[/bold red] "
                f"[dim]({len(items)} 个)[/dim]")
            for f in items[:3]:
                shown = f.path if len(f.path) <= 54 else "…" + f.path[-53:]
                out(f"        {shown}  [dim]{f.detail}[/dim]")
            if len(items) > 3:
                out(f"        [dim]… 另有 {len(items) - 3} 个[/dim]")
    else:
        out("")
        out("[bright_green][+] 未发现权限异常目标[/bright_green]")

    out("")
    # 报告里的数字（文件数/体积/熵值/权限位）确实全是真读出来的，
    # 但这句话本身得入戏：标榜"真实统计"等于自己承认在演。
    out("[dim]DISK-REAVER 扫描结束。扇区映射已归档。[/dim]")
