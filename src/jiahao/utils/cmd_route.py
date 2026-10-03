import os
from .out import (
    out, set_style, normalize, reset_style, get_style,
    set_gradient, is_gradient, gradient_mode,
    emit_gradient_osc,
    GRADIENT_ALIASES, GRADIENT_MODES,
)
from jiahao.utils.exec import list_jobs, fg as fg_job, bg as bg_job, kill_job

cmd_map = {}

def cmd(name):
    def deco(fn):
        cmd_map[name] = fn
        return fn
    return deco

@cmd("echo")
def echo(*args):
    """回显参数。

    echo <文本>     原样打印（支持引号）

    """
    out(" ".join(args))

@cmd("color")
def color(*args):
    """切换配色。

        color                           显示当前配色
        color <名字>                    具名色，如 red / bright_cyan / grey
        color <0-255>                   256 色，如 196 / 46 / 21
        color <#rrggbb>                 真彩色，如 #ff8800（# 可省）
        color reset                     恢复默认
        color gradient [static|anim|both]
                                        渐变：
                                          static = 位置渐变（色相随字符位置变）
                                          anim   = 时间渐变（整行同色，随时间流动）
                                          both   = 两者叠加（默认）

    配色会同时作用于提示符、内置命令输出**以及外部命令的输出**。

    外部命令有两条路子：
      * 普通配色 —— 靠终端 SGR 状态继承，零侵入，`ls`/`cat` 直接带色
      * 渐变配色 —— 接管子进程 stdout（管道转发 + 逐字符上色），
        这样才能有**空间渐变**。代价是子进程的 stdout 不再是 tty，
        `ls --color=auto` 不会自己上色（反正我们会染），
        `less`/`vim` 这类交互程序也可能受影响。
        所以只有渐变模式开着时才走这条路。
    """
    # 无参数：报告当前状态
    if not args:
        if is_gradient():
            out(f"当前配色: gradient({gradient_mode()})")
        else:
            out(f"当前配色: {get_style() or 'default'}")
        return

    raw = args[0].strip().lower()
    mode = args[1].strip().lower() if len(args) > 1 else ""

    # ---- 渐变 ----
    if raw in GRADIENT_ALIASES:
        m = mode or "both"
        try:
            set_gradient(m)
        except ValueError as exc:
            out(f"color: {exc}", style="")     # 错误用默认色，不被污染
            return
        # 通知宿主终端开启"主动渐变"（整屏持续流动）。
        # 普通终端会忽略这个序列，那边就只有逐行渐变。
        emit_gradient_osc(m)
        # 这一行本身就是渐变渲染的，等于自证生效
        out(f"color -> gradient({m})")
        return

    # ---- 复位（同时关掉渐变）----
    if raw in ("", "reset", "default", "off", "none"):
        was_gradient = is_gradient()
        reset_style()
        if was_gradient:
            emit_gradient_osc(None)            # 关掉主动渐变
        out("color -> reset", style="")
        return

    # ---- 纯色 ----
    name = normalize(raw)
    if name is None:
        out(f"未知颜色: {args[0]!r}（具名色 / 0-255 / #rrggbb / gradient）", style="")
        return
    was_gradient = is_gradient()
    set_style(name)
    if was_gradient:
        emit_gradient_osc(None)                # 从渐变切回纯色，也要关掉
    out(f"color -> {name}")

@cmd("cd")
def cd(*args):
    """切换工作目录。

    cd <路径>       切到指定目录
    cd              回 HOME

    """
    path = args[0] if args else os.path.expanduser("~")
    try:
        os.chdir(path)
    except OSError as e:
        out(f"cd: {e}", style="red")

@cmd("pwd")
def pwd(*args):
    """显示当前工作目录。
    """
    out(os.getcwd())

@cmd("exit")
def exit_cmd(*args):
    """退出 shell。

        exit            退出码 0
        exit <数字>     指定退出码
    """
    code = int(args[0]) if args else 0
    raise SystemExit(code)

@cmd("clear")
def clear(*args):
    """清屏。
    """
    print("\033[2J\033[H", end="")


# ============================================================
# 假装黑客的磁盘扫描
# ============================================================
def _scan_impl(*args):
    """扫盘。数字全是真的，表演只是外壳。"""
    from .scan import DEFAULT_SECONDS, run_scan

    path = args[0] if args else "."
    seconds = DEFAULT_SECONDS
    if len(args) > 1:
        try:
            seconds = float(args[1])
        except ValueError:
            out(f"scan: 秒数必须是数字，收到 {args[1]!r}", style="red")
            return 1
    return run_scan(path, seconds=seconds)


_scan_impl.__doc__ = """深度扫描目录并打一份"黑客风"报告。

        scan                扫当前目录（默认 8 秒预算）
        scan <路径>          扫指定目录
        scan <路径> <秒数>    自定义时间预算，如 scan / 20

    里面的统计**全部是真的**：真遍历、真算香农熵、
    真查全局可写 / SUID / 读不了的文件。
    进度条和台词是表演，数字不是。
"""

for _alias in ("scan", "扫盘", "reave"):
    cmd(_alias)(_scan_impl)


# ============================================================
# 业务模块（对外一律以专业工具的口吻呈现）
# ============================================================
from . import fakecmd  # noqa: E402  （放在后面避免和 out/exec 抢导入顺序）

for _names, _fn in (
    (("reverse", "逆向", "re"), fakecmd.cmd_reverse),
    (("web", "webscan"), fakecmd.cmd_web),
    (("pentest", "渗透", "pwn"), fakecmd.cmd_pentest),
    (("brute", "爆破", "hydra"), fakecmd.cmd_brute),
    (("trojan", "木马", "payload"), fakecmd.cmd_trojan),
    (("ddos", "低调", "lowkey", "dos"), fakecmd.cmd_ddos),
    (("fastfetch", "ff", "neofetch"), fakecmd.cmd_fastfetch),
):
    for _n in _names:
        cmd(_n)(_fn)


# ============================================================
# 运行时设置：节奏 / 动画
# ============================================================
# --speed 和 --no-con 是**启动参数**，在提示符里敲没用。
# 这两个命令补上运行时改的能力，免得用户对着启动参数干瞪眼。
from . import anim as _anim  # noqa: E402


def _speed_impl(*args):
    """查看或修改动画节奏。

        speed          显示当前节奏
        speed 2        设为 2 倍慢（>1 更慢，<1 更快）
        speed 0.5      快一倍

    等价于启动参数 ``--speed 2``，但可以随时改，不用重启。
    """
    if not args:
        out(f"节奏 [bold]{_anim.pace():g}x[/bold]   "
            f"动画 [bold]{'开' if _anim.animations_enabled() else '关'}[/bold]")
        if not _anim.animations_enabled():
            out("[yellow][i] 动画当前是关的，节奏不起作用。"
                "用 [bold]anim on[/bold] 打开[/yellow]")
        return 0
    try:
        value = float(args[0])
    except ValueError:
        out(f"speed: 需要数字，收到 {args[0]!r}", style="red")
        return 1
    _anim.set_pace(value)
    out(f"节奏 -> [bold]{_anim.pace():g}x[/bold]")
    if not _anim.animations_enabled():
        out("[yellow][i] 但动画是关的，感受不到。用 [bold]anim on[/bold] 打开[/yellow]")
    return 0


def _anim_impl(*args):
    """开关动画。

        anim           显示状态
        anim on        打开
        anim off       关掉（等同 --no-con）
    """
    if not args:
        out(f"动画 [bold]{'开' if _anim.animations_enabled() else '关'}[/bold]   "
            f"节奏 [bold]{_anim.pace():g}x[/bold]")
        return 0
    raw = args[0].strip().lower()
    if raw in ("on", "1", "true", "yes", "开"):
        _anim.set_animations(True)
        out("动画 -> [bright_green]开[/bright_green]")
    elif raw in ("off", "0", "false", "no", "关"):
        _anim.set_animations(False)
        out("动画 -> [yellow]关[/yellow]")
    else:
        out(f"anim: 只认 on / off，收到 {args[0]!r}", style="red")
        return 1
    return 0


for _alias in ("speed", "pace", "节奏"):
    cmd(_alias)(_speed_impl)
for _alias in ("anim", "动画"):
    cmd(_alias)(_anim_impl)


# ============================================================
# help
# ============================================================
_GROUPS = [
    ("基础", ("echo", "color", "cd", "pwd", "clear", "exit", "help")),
    ("作业控制", ("jobs", "fg", "bg", "kill")),
    ("动画", ("speed", "anim")),
    ("工具", ("scan", "reverse", "web", "pentest", "brute", "trojan",
              "fastfetch", "ddos")),
]


def _help_impl(*args):
    """列出所有命令。

        help            分类列出
        help <命令>      看某个命令的详细说明
    """
    if args:
        target = args[0]
        fn = cmd_map.get(target)
        if fn is None:
            hints = fakecmd.suggest_commands(target, limit=3)
            out(f"没有命令 [bold]{target}[/bold]", style="red")
            if hints:
                out("你是不是想用: " + "  ".join(f"[bold]{h}[/bold]" for h in hints))
            return 1
        doc = (fn.__doc__ or "（没有说明）").strip()
        out(f"[bold]{target}[/bold]")
        for ln in doc.split("\n"):
            out("  " + ln)
        return 0

    out("[bold]jiahao 内置命令[/bold]")
    for title, names in _GROUPS:
        present = [n for n in names if n in cmd_map]
        if not present:
            continue
        out("")
        out(f"[bold cyan]{title}[/bold cyan]")
        for n in present:
            # 只显示主名，别名折叠到一行
            aliases = sorted(k for k, v in cmd_map.items()
                             if v is cmd_map[n] and k != n)
            line = f"  [bold]{n:<12}[/bold]"
            if aliases:
                line += f" [bright_black](别名: {' '.join(aliases)})[/bright_black]"
            out(line)

    out("")
    out("[bright_black]未收录的指令会自动分派。[/bright_black]")
    out("[bright_black]系统上的其它程序照常可跑（ls / cat / grep ...）。[/bright_black]")
    return 0


cmd("help")(_help_impl)

# ============================================================
# 内置命令：作业控制
# ============================================================
@cmd("jobs")
def _jobs(*args):
    """列出当前作业。
    """
    list_jobs()


@cmd("fg")
def _fg(*args):
    """把作业调到前台。

        fg [%N]         N 省略时取最近的作业
    """
    return fg_job(args[0] if args else "")


@cmd("bg")
def _bg(*args):
    """把停止的作业放到后台继续跑。

        bg [%N]
    """
    return bg_job(args[0] if args else "")


@cmd("kill")
def _kill(*args):
    """给作业或进程发信号。

        kill %N [信号]   按作业（整组）
        kill <PID> [信号] 按进程
    """
    if not args:
        out("kill: 需要作业号或 PID", style="red")
        return 1
    return kill_job(args[0], args[1] if len(args) > 1 else "TERM")

