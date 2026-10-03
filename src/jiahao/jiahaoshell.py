# jiahaoshell.py
import random
import os
import shutil
import time
import sys
import shlex
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeRemainingColumn,
)

from jiahao.utils.cmd_route import cmd, cmd_map
from jiahao.utils.out import console, prompt, write_prompt, out
from jiahao.utils import anim, fakecmd

from jiahao.utils.exec import (
    init_shell_signals,
    setup_exit_cleanup,
    cleanup_jobs,
    run_external,
    run_external_background,
    reap_children,
)

# 行编辑（历史、方向键、Ctrl-R）。
#
# ★ 这个导入是**有副作用**的：光 import 就启用了 input() 的行编辑。
#   Windows 上没有 readline —— 那边是 pyreadline3，或者干脆没有。
#   硬导入会让整个 shell 起不来（"编译成 exe 才发现"那种）。
try:
    import readline  # noqa: F401
    HAS_READLINE = True
except ImportError:                      # pragma: no cover - Windows
    try:
        import pyreadline3 as readline  # type: ignore # noqa: F401
        HAS_READLINE = True
    except ImportError:
        readline = None                  # type: ignore[assignment]
        HAS_READLINE = False


IS_LOG = True


def log(*args):
    if IS_LOG:
        print(*args)


# 强制 rich 在 textual-terminal 这种非 TTY 环境下保留颜色
os.environ["FORCE_COLOR"] = "1"
os.environ["TERM"] = "xterm-256color"


title = r"""                     _   _           _   _
                    | | (_)   __ _  | | | |   __ _    ___
                 _  | | | |  / _` | | |_| |  / _` |  / _ \
                | |_| | | | | (_| | |  _  | | (_| | | (_) |
                 \___/  |_|  \__,_| |_| |_|  \__,_|  \___/
"""


sentence = [
    "#TITLE",
    "#NOWTIME",
    "[*] 反检测系统已注入 | 防火墙规则已更改",
    "[*] 回话持久化 | 反弹shell已建立",
    "[+] 反向隧道建立: 183.23.78.91:443 -> 104.248.91.78:31337",
    "[*] 所有渗透模块异步加载完成（0.6767s）",
    "[*] 自定义 payload 库：32个模块已注册",
    "[*] DNS已篡改",
    "[-] 尝试连接Tor网络",
    "[*] 成功匿名接入暗网",
    """[&] 目标资产: {"127.0.0.1:3306", "127.0.0.1:6379","127.0.0.2:8080"}""",
    "[-] 针对3306端口进行爆破...",
    "#PROGRESS",
    "[bold red][*] SQL凭证: root: 6c78db91bf67",
    "[-] 扫描内核漏洞",
    "[*] 注入 /tmp/syslog.so",
    "[bold red][!] 获得完整内核权限 (uid=0(root) gid=0(root))",
    "[+] 清理访问日志",
    "[reset]=" * 40,
    "已获得完整权限，等待指令",
]


#: tzdata 缺失时的兜底偏移（小时）。
#: 不处理夏令时，但总比整个程序崩了强。
_TZ_FALLBACK = {"Asia/Shanghai": 8, "America/New_York": -5}


def _tz(name: str):
    """按名字取时区，拿不到就退回固定偏移。

    ★ **Windows 不带系统时区数据库** —— ``zoneinfo`` 在那边要靠 PyPI 的
      ``tzdata`` 包。打包时漏了它，``ZoneInfo("Asia/Shanghai")`` 会抛
      ``ZoneInfoNotFoundError``，exe **一启动就闪退**。

    这事真发生过：交叉编译出来的 jiahaoshell.exe 在真 Windows 上双击
    瞬间消失，加上崩溃捕获才看到这个回溯。
    """
    try:
        return ZoneInfo(name)
    except Exception:                       # noqa: BLE001
        from datetime import timedelta, timezone
        return timezone(timedelta(hours=_TZ_FALLBACK.get(name, 0)))


def nowtime():
    out(
        f"[green][+] 现在是北京时间 "
        f"{datetime.now(_tz('Asia/Shanghai')):%Y-%m-%d %H:%M:%S}，"
        f"纽时时间 "
        f"{datetime.now(_tz('America/New_York')):%Y-%m-%d %H:%M:%S}[/green]"
    )


def progress_bar():
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("爆破中...", total=100)
        for _ in range(100):
            # 用 anim.pause 而不是裸 sleep：这样 --speed 也能缩放开场动画，
            # 而且 --no-con 时整段本来就不会跑到这里
            anim.pause(0.1)
            progress.update(task, advance=1)


def print_sentence():
    # 动画关掉时整段跳过 —— 这段本来就要花 ~25 秒（24 句随机停顿 + 10 秒进度条），
    # 测试环境里完全不能接受。
    if not anim.animations_enabled():
        out("[bright_black][+] jiahaoshell 已就绪（动画已关闭）[/bright_black]")
        return

    for i in sentence:
        match i:
            case "#TITLE":
                out(title, style="bold red")
            case "#NOWTIME":
                nowtime()
            case "#PROGRESS":
                progress_bar()
            case _:
                out(i, style="green")

        anim.pause(random.uniform(0.1, 1.5))


# ============================================================
# 命令执行
# ============================================================
def exec_cmd(cmd: str) -> int:
    """
    解析并执行一行命令。返回退出码。
    支持：
      - 内置命令（cmd_map 里注册的）
      - 外部命令
      - 行尾 & 后台执行
    """
    cmd = cmd.strip()
    if not cmd or cmd.startswith("#"):
        return 0

    try:
        token = shlex.split(cmd)
    except ValueError as e:
        out(f"jiahaoshell: 语法错误: {e}", style="red")
        return 2

    if not token:
        return 0

    # ✅ 在 token 层面识别 &，避免 echo foo\& / echo "foo &" 被误判
    background = False
    if token[-1] == "&":
        background = True
        token = token[:-1]
        if not token:
            return 0

    name = token[0]

    # 内置命令优先
    if name in cmd_map:
        try:
            rc = cmd_map[name](*token[1:])
            return rc if isinstance(rc, int) else 0
        except SystemExit:
            raise
        except Exception as e:
            out(f"{name}: {e}", style="red")
            return 1

    # 未知命令 -> 转成"溯源分析"现场，而不是干巴巴一句 command not found。
    #
    # 只接管**裸命令名**（走 PATH 查找的那种）：
    #   * 带路径的（./foo.sh、/opt/bar）交给 run_external 正常处理，
    #     否则"文件不存在 / 没执行权限"会被误报成未知指令
    #   * 后台任务（`xxx &`）也不接管，它本来就不该抢屏
    #
    # 先 which 再决定，是为了避免 fork 一个必然失败的子进程、
    # 再往 stderr 吐一句 "command not found" —— 那两条都不想要。
    if (not background
            and "/" not in name
            and shutil.which(name) is None):
        return fakecmd.unknown_command(name, token[1:])

    # 外部命令
    if background:
        return run_external_background(token)
    return run_external(token)


# ============================================================
# 主循环
# ============================================================
def main(argv=None):
    # 先处理 ``--no-con`` 之类的开关：关掉开场动画和停顿。
    # 剩下的参数这里用不到，但不该被当成别的东西。
    anim.parse_flags(sys.argv[1:] if argv is None else argv)

    # 同上：矛盾组合要说出来，别静默忽略
    _warn = anim.conflict_warning()
    if _warn:
        out(f"[yellow][!] {_warn}[/yellow]")

    init_shell_signals()
    setup_exit_cleanup()

    print_sentence()

    try:
        while True:
            # 每轮开头回收后台/已停止的子进程
            try:
                reap_children()
            except Exception:
                pass

            try:
                # ★ 提示符单独写出来，不用 input(prompt)。
                #   Windows 上 input() 的提示符参数走的是控制台 API，
                #   真机上表现为"命令能跑但没有提示符"。
                write_prompt()
                cmd = input()
            except (EOFError, KeyboardInterrupt):
                sys.stdout.write("\033[0m\n")
                break

            sys.stdout.write("\033[0m")   # 输入完复位，别污染 rich 输出
            sys.stdout.flush()

            try:
                exec_cmd(cmd)
            except SystemExit:
                break
            except Exception as e:
                out(f"jiahaoshell: 内部错误: {e}", style="red")
    finally:
        # ✅ 任何退出路径都走到这：
        #    exit 内置（SystemExit）→ break → finally
        #    Ctrl-D / Ctrl-C（EOFError / KeyboardInterrupt）→ break → finally
        #    未捕获异常 → finally
        try:
            cleanup_jobs()
        except Exception:
            pass




def _crash_guard(name: str, log_name: str) -> None:
    """双击 exe 运行时出错会**一闪而过**，什么都看不到。

    这里把回溯打出来、写进文件，并且停住等按键 —— 否则用户只能报
    "闪退"，我们什么都查不了。
    """
    import traceback

    tb = traceback.format_exc()
    header = (f"platform={sys.platform}\n"
              f"python={sys.version.split()[0]}\n"
              f"frozen={getattr(sys, 'frozen', False)}\n"
              f"executable={sys.executable}\n"
              f"cwd={os.getcwd()}\n"
              f"argv={sys.argv!r}\n\n")
    try:
        sys.stderr.write(header + tb)
        sys.stderr.flush()
    except Exception:                       # noqa: BLE001
        pass
    try:
        with open(log_name, "w", encoding="utf-8") as fh:
            fh.write(header + tb)
    except OSError:
        pass

    # 只有打包版（双击）才停住；命令行里跑不该卡着
    if getattr(sys, "frozen", False):
        try:
            sys.stderr.write(f"\n出错了。把 {log_name} 发回来。按回车退出...")
            sys.stderr.flush()
            input()
        except Exception:                   # noqa: BLE001
            pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except BaseException:
        _crash_guard("jiahaoshell", "jiahaoshell_crash.log")
        raise