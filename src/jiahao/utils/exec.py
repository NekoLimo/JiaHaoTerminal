# jiahao/utils/exec.py
from __future__ import annotations
import codecs
import select
import threading
import time
import atexit
import os
import sys
import signal
import subprocess
import shlex
#: 这个平台有没有 fork / 进程组。
#:
#: Windows 没有，那边得走 CreateProcess + 作业对象。全套作业控制
#: （fg/bg/jobs/kill %N）是 POSIX 专有的，这里只做**降级**：
#: 前台外部命令照跑（走 subprocess），后台作业直接说明不支持。
HAVE_FORK = hasattr(os, "fork") and hasattr(os, "killpg")

#: Windows 上不存在的信号，用 getattr 兜底
_SIGKILL = getattr(signal, "SIGKILL", None)
_SIGHUP = getattr(signal, "SIGHUP", None)
_SIGCONT = getattr(signal, "SIGCONT", None)


def _subprocess_fallback(token: list[str]) -> int:
    """没有 fork 时的前台执行路径。

    丢掉的东西：管道转发上色（那条路要 fork）、作业控制。
    保住的东西：命令能跑、退出码能透传、输出进终端。
    在 Windows 上"能跑"比"有颜色"重要。
    """
    import subprocess
    try:
        return subprocess.run(token).returncode
    except FileNotFoundError:
        _child_error(token[0], "command not found")
        return 127
    except PermissionError:
        _child_error(token[0], "permission denied")
        return 126
    except OSError as exc:
        _child_error(token[0], str(exc))
        return 126


from jiahao.utils.out import (
    out, emit_style_prefix, emit_style_reset,
    wants_stdout_relay, StreamPainter, child_style_prefix, ANSI_RESET,
)


#: 这些程序需要**真终端**才能正常工作（分页器、全屏编辑器、监视器、
#: 远程登录、多路复用、REPL……）。即使设了配色也不接管它们的 stdout，
#: 否则它们会因为看到管道而关掉交互模式。
#:
#: 跳过不等于没颜色：这条路会退回"SGR 状态继承"，对不自个儿上色的
#: 程序（比如 python 脚本）照样有颜色。
RELAY_SKIP = frozenset({
    # 分页器
    "less", "more", "most", "pg",
    # 编辑器
    "vim", "vi", "nvim", "nano", "emacs", "micro", "pico", "joe", "mcedit",
    # 监视器
    "top", "htop", "btop", "atop", "watch",
    # 远程
    "ssh", "mosh", "telnet", "sftp", "ftp", "rdesktop",
    # 多路复用
    "tmux", "screen", "zellij", "dvtm",
    # REPL
    "python", "python3", "ipython", "node", "irb", "pry", "lua", "ruby",
    "sqlite3", "psql", "mysql", "redis-cli", "mongosh",
    # 全屏 TUI
    "fzf", "dialog", "whiptail", "mc", "ncdu", "ranger", "nnn",
    # 调试器
    "gdb", "lldb", "pdb",
    # 手册
    "man", "info",
})


def _basename(cmd: str) -> str:
    """取命令名（去掉路径），用于查 RELAY_SKIP。"""
    return cmd.rsplit("/", 1)[-1] if "/" in cmd else cmd


def _child_error(name: str, message: str) -> None:
    """子进程里的 exec 失败报错。

    **自带颜色**：前台还能靠父进程设好的环境 SGR 状态上色，后台任务
    没有那东西（父进程不能为异步的子进程维持终端状态），所以这里
    直接把自己那份转义写出去。

    走 fd 2（stderr），保持和真实 shell 一致的语义。
    """
    try:
        prefix = child_style_prefix()
        reset = ANSI_RESET if prefix else ""
        os.write(2, f"{prefix}jiahaoshell: {name}: {message}{reset}\n"
                 .encode("utf-8", "replace"))
    except OSError:
        pass


# ============================================================
# 作业表
# ============================================================
# jobs[pid] = {
#     "cmd":    str,
#     "status": "running" | "stopped" | "background",
#     "jid":    int,
#     "seq":    int,   # 单调递增，越大越"最近"，用于 +/- 标记
# }
#
# 注意：当前实现里 fork 后立刻 setpgid(pid, pid)，所以 pid == pgid。
# 做管道时字段会改名为 pgid，那时 pid != pgid。
jobs: dict[int, dict] = {}

_job_id_counter = [0]
_job_seq_counter = [0]


def _next_job_id() -> int:
    _job_id_counter[0] += 1
    return _job_id_counter[0]


def _next_seq() -> int:
    _job_seq_counter[0] += 1
    return _job_seq_counter[0]


def _register_job(pid: int, token: list[str], status: str) -> int:
    jid = _next_job_id()
    try:
        cmd_str = shlex.join(token)               # Python 3.8+
    except AttributeError:
        cmd_str = " ".join(shlex.quote(a) for a in token)
    jobs[pid] = {
        "cmd":    cmd_str,
        "status": status,
        "jid":    jid,
        "seq":    _next_seq(),
    }
    return jid


def _bump_job(pid: int) -> None:
    if pid in jobs:
        jobs[pid]["seq"] = _next_seq()


# ============================================================
# 信号 / 终端
# ============================================================
def _set_signal(name: str, handler) -> bool:
    """按名字设信号处理；平台没有这个信号就安静跳过。

    Windows 上 ``SIGQUIT``/``SIGTSTP``/``SIGTTIN``/``SIGTTOU``/``SIGHUP``
    都不存在，直接 ``signal.signal`` 会抛 AttributeError，
    一个信号就让整个 shell 起不来，不划算。
    """
    sig = getattr(signal, name, None)
    if sig is None:
        return False
    try:
        signal.signal(sig, handler)
    except (OSError, ValueError, RuntimeError):
        # 非主线程、或该信号不可捕获 —— 都不是致命问题
        return False
    return True


def init_shell_signals() -> None:
    """
    主进程启动时调一次。shell 忽略所有作业控制信号，
    由前台进程组去处理。
    """
    for _name in ("SIGINT", "SIGQUIT", "SIGTSTP", "SIGTTIN", "SIGTTOU"):
        _set_signal(_name, signal.SIG_IGN)
    # SIGCHLD 保持默认，不要设成 SIG_IGN —— 那会让 waitpid 失效


_exit_cleanup_installed = False


def setup_exit_cleanup() -> None:
    """
    安装退出清理。可重复调用（幂等）。
    行为：
      - 忽略 SIGHUP：终端关闭时不立刻被内核带走，让 atexit 有机会跑
      - 注册 atexit：任何正常/异常退出路径都会给剩余作业发 SIGHUP
    终端关闭后的退出链：
      终端关 → SIGHUP（被忽略）→ stdin 关闭 → input() 抛 EOFError
      → 主循环 break → 解释器退出 → atexit 跑 cleanup_jobs
    """
    global _exit_cleanup_installed
    _set_signal("SIGHUP", signal.SIG_IGN)
    if not _exit_cleanup_installed:
        atexit.register(cleanup_jobs)
        _exit_cleanup_installed = True


def cleanup_jobs() -> None:
    """
    shell 退出前清理所有作业。
    关键点：
      1. 先 SIGCONT 唤醒（stopped 进程收不到其他信号）
      2. 再 SIGTERM 请它退出
      3. 短超时后 SIGKILL 兜底
    """
    pids = list(jobs)
    if not pids:
        return

    # ---- 第一步：唤醒 + 请求退出 ----
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        except OSError as e:
            out(f"cleanup: SIGCONT {pid} 失败: {e}", style="yellow")

        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as e:
            out(f"cleanup: SIGTERM {pid} 失败: {e}", style="yellow")

    # ---- 第二步：给 200ms 让它自然死 ----
    deadline = time.monotonic() + 0.2
    pending = set(pids)
    while pending and time.monotonic() < deadline:
        for pid in list(pending):
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
                if done == pid:
                    pending.discard(pid)
            except ChildProcessError:
                pending.discard(pid)
            except OSError:
                pending.discard(pid)
        time.sleep(0.02)

    # ---- 第三步：还活着的强杀 ----
    for pid in pending:
        try:
            if _SIGKILL is None:          # pragma: no cover - Windows
                continue
            os.killpg(pid, _SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as e:
            out(f"cleanup: SIGKILL {pid} 失败: {e}", style="yellow")

    jobs.clear()          # 幂等：下次跑就空循环了


def _has_tty() -> bool:
    """
    用 stdin 是否 tty 作为"是否有控制终端"的近似判断。
    局限：stdin 被重定向、stdout 是 tty 时，会跳过 tcsetpgrp，
    前台命令的 Ctrl-C 传不下去。真实 shell 会 open(ctermid()) 单独拿 fd，
    这里保持简单。
    """
    return sys.stdin.isatty()


def _give_terminal_to(pgid: int) -> None:
    if not _has_tty():
        return
    try:
        os.tcsetpgrp(sys.stdin.fileno(), pgid)
    except OSError:
        pass


def _take_terminal_back() -> None:
    if not _has_tty():
        return
    try:
        os.tcsetpgrp(sys.stdin.fileno(), os.getpgrp())
    except OSError:
        pass


def _reset_child_signals() -> None:
    for _name in ("SIGINT", "SIGQUIT", "SIGTSTP", "SIGTTIN", "SIGTTOU",
                  "SIGCHLD", "SIGPIPE"):
        _set_signal(_name, signal.SIG_DFL)


# ============================================================
# 前台执行
# ============================================================
def _relay_colored(read_fd: int, painter: StreamPainter) -> None:
    """把管道里的内容逐字符上色后转发到真正的 stdout。

    跑在独立线程里，这样前台的作业控制（waitpid / tcsetpgrp / Ctrl+Z）
    可以完全不变 —— 子进程被停住时这个线程只是阻塞在 select 上，
    恢复后继续转发，不会丢输出。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending: list[str] = []

    def flush() -> None:
        if pending:
            try:
                os.write(1, "".join(pending).encode("utf-8"))
            except OSError:
                pass
            pending.clear()

    try:
        while True:
            try:
                ready, _, _ = select.select([read_fd], [], [], 0.5)
            except InterruptedError:
                continue
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                chunk = os.read(read_fd, 65536)
            except (InterruptedError, BlockingIOError):
                continue
            except OSError:
                break
            if not chunk:
                break                      # 写端全部关闭 = 输出结束
            text = decoder.decode(chunk)
            if text:
                pending.append(painter.paint(text))
                if len(pending) >= 16:
                    flush()

        tail = decoder.decode(b"", final=True)
        if tail:
            pending.append(painter.paint(tail))
        end = painter.finish()
        if end:
            pending.append(end)
        flush()
    except Exception:                       # noqa: BLE001 - 转发线程不能把主流程带崩
        pass
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass


def capture_external(token: list[str], timeout: float = 6.0) -> bytes | None:
    """跑一个外部命令并抓回它的 stdout；跑不起来或者**没输出**都返回 None。

    ★ 和 ``run_external`` 的区别是"要拿到结果才能判断"。

    以前 ``ff`` 是直接转交的 —— 假设"vendor 里有这个二进制，它就一定能跑"。
    **这个假设是错的**：缺 DLL、架构不匹配、被杀软拦下、在 Wine 里跑不动……
    都会让进程**正常退出但一个字节都不输出**，用户屏幕上就是一片空白，
    而且完全没有提示。（真出过：Wine 里跑官方 Windows 版 fastfetch
    就是 0 输出 0 退出码。）

    ★ 放在 ``exec`` 里而不是 ``fakecmd`` 里，是因为 ``fakecmd`` 有一条
      "业务模块不许直接起进程"的约束（那边大部分命令是表演性的）。
      要跑真东西就走这儿。
    """
    if not token:
        return None
    try:
        proc = subprocess.run(token, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None

    data = proc.stdout or b""
    if not data.strip():
        return None                     # 空输出 = 不可用，让调用方回退
    return data


def run_external(token: list[str]) -> int:
    if not token:
        return 0

    # 没有 fork 的平台（Windows）走 subprocess 降级路径
    if not HAVE_FORK:                     # pragma: no cover - Windows
        return _subprocess_fallback(token)

    # 给外部命令的输出上色，两条路子：
    #
    # 1) SGR 状态继承：fork 之前把颜色写进终端，子进程的输出自动继承。
    #    零侵入，但**只有子进程不自己上色时才有效**。
    # 2) 接管 stdout（管道转发）：逐字符上色。`tree` / `ls --color` 这类
    #    程序在 stdout 是终端时会自己上色，并且逐条发 ESC[0m 把我们的
    #    SGR 状态整个清掉 —— 所以只要设了配色就得接管，否则会出现
    #    "设了颜色却没颜色"。
    #
    # 交互式程序（分页器/编辑器/REPL…）不能接管，它们在 RELAY_SKIP 里，
    # 退回路子 1。
    relay = wants_stdout_relay() and _basename(token[0]) not in RELAY_SKIP

    read_fd = write_fd = None
    if relay:
        try:
            read_fd, write_fd = os.pipe()
        except OSError:
            relay = False

    # ★ fork 前把颜色写进终端，让**子进程的 stderr** 也带上颜色。
    #
    # 管道转发只管得到 stdout；`command not found`、权限错误、程序自己
    # 的报错都是写 stderr 的，直接进终端，只能靠这个 SGR 状态上色。
    #
    # 走转发时写到 fd 2 而不是 fd 1 —— stdout 那边由 StreamPainter 逐字符
    # 上色，环境 SGR 只会往 stdout 里塞一段没用的转义。
    color_fd = 2 if relay else 1
    emit_style_prefix(color_fd)

    pid = os.fork()

    if pid == 0:
        # ---------------- 子进程 ----------------
        try:
            _reset_child_signals()
            if relay:
                os.close(read_fd)
                os.dup2(write_fd, 1)
                os.close(write_fd)
            try:
                os.setpgid(0, 0)
            except OSError:
                pass
            # ⚠️ 不要在这里 tcsetpgrp：会吃 SIGTTOU 自杀
            os.execvp(token[0], token)
        except FileNotFoundError:
            _child_error(token[0], "command not found")
            os._exit(127)
        except PermissionError:
            _child_error(token[0], "permission denied")
            os._exit(126)
        except OSError as e:
            _child_error(token[0], str(e))
            os._exit(126)
        except Exception as e:
            _child_error(token[0], str(e))
            os._exit(1)

    # ---------------- 父进程 ----------------
    relay_thread = None
    if relay:
        os.close(write_fd)
        relay_thread = threading.Thread(
            target=_relay_colored,
            args=(read_fd, StreamPainter()),
            name="jiahao-color-relay",
            daemon=True,
        )
        relay_thread.start()

    try:
        os.setpgid(pid, pid)
    except OSError:
        pass

    _give_terminal_to(pid)

    try:
        try:
            _, status = os.waitpid(pid, os.WUNTRACED)
        except ChildProcessError:
            out(f"jiahaoshell: 进程 {pid} 已不存在", style="yellow")
            return 1
    finally:
        _take_terminal_back()
        if relay_thread is not None:
            # 等转发线程把剩余输出刷完，避免和后面的提示符交错。
            # 子进程被 Ctrl+Z 停住时线程会一直阻塞在 select 上，
            # 所以必须带超时，绝不能无限等。
            relay_thread.join(timeout=1.0)
        # 复位上面那个 SGR 状态，否则后面的提示符和输出会被一起染上
        emit_style_reset(color_fd)

    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)

    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        if sig == signal.SIGINT:
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            out(f"被信号 {sig} 终止", style="yellow")
        return 128 + sig

    if os.WIFSTOPPED(status):
        sig = os.WSTOPSIG(status)
        jid = _register_job(pid, token, "stopped")
        out(f"[{jid}]+ 已停止 (sig {sig})  {jobs[pid]['cmd']}", style="yellow")
        return 128 + sig

    return 0


# ============================================================
# 后台执行
# ============================================================
def run_external_background(token: list[str]) -> int:
    if not token:
        return 0

    # 后台作业依赖 fork + 进程组（setpgid/killpg），Windows 没有。
    # 与其半吊子地跑起来收不回去，不如说清楚。
    if not HAVE_FORK:                     # pragma: no cover - Windows
        out("本平台不支持后台作业（需要 fork / 进程组）", style="red")
        return 1

    pid = os.fork()

    if pid == 0:
        # ---------------- 子进程 ----------------
        try:
            _reset_child_signals()
            try:
                os.setpgid(0, 0)
            except OSError:
                pass
            os.execvp(token[0], token)
        except FileNotFoundError:
            _child_error(token[0], "command not found")
            os._exit(127)
        except PermissionError:
            _child_error(token[0], "permission denied")
            os._exit(126)
        except OSError as e:
            _child_error(token[0], str(e))
            os._exit(126)

    # ---------------- 父进程 ----------------
    try:
        os.setpgid(pid, pid)
    except OSError:
        pass

    jid = _register_job(pid, token, "background")
    out(f"[{jid}] {pid}", style="yellow")
    return 0


# ============================================================
# 回收子进程
# ============================================================
def reap_children() -> None:
    """
    主循环每轮调一次。回收结束/停止的子进程。
    不带 WCONTINUED：状态变化全部由 fg/bg 自己维护。
    """
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            break
        except OSError:
            break

        if pid == 0:
            break

        info = jobs.get(pid)

        if os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
            if info:
                out(
                    f"[{info['jid']}]+ 已完成 (exit {code})  {info['cmd']}",
                    style="yellow",
                )
                jobs.pop(pid, None)

        elif os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            if info:
                out(
                    f"[{info['jid']}]+ 被信号 {sig} 终止  {info['cmd']}",
                    style="yellow",
                )
                jobs.pop(pid, None)

        elif os.WIFSTOPPED(status):
            # 任何停止信号（TSTP / TTIN / TTOU / STOP）都更新状态
            sig = os.WSTOPSIG(status)
            if info:
                info["status"] = "stopped"
                _bump_job(pid)
                out(
                    f"[{info['jid']}]+ 已停止 (sig {sig})  {info['cmd']}",
                    style="yellow",
                )


# ============================================================
# 作业显示
# ============================================================
def list_jobs() -> None:
    if not jobs:
        return

    ordered = sorted(jobs.items(), key=lambda kv: kv[1]["seq"])
    n = len(ordered)

    for idx, (_pid, info) in enumerate(ordered):
        if idx == n - 1:
            marker = "+"
        elif idx == n - 2:
            marker = "-"
        else:
            marker = " "
        out(
            f"[{info['jid']}]{marker}  {info['status']:10}  {info['cmd']}",
            style="yellow",
        )


# ============================================================
# 内置：fg / bg / kill
# ============================================================
def fg(job_id: str = "") -> int:
    """
    作业引用约定（宽松）：
      "" / "%" / "%+" / "%-" / "%N"  —— 标准形式
      "N"（裸数字）                    —— 也按 jid 匹配
    注意与 kill_job 的差异：
      kill_job 的裸数字按 PID 解释，fg/bg 的裸数字按 jid 解释。
      这是有意为之，因为 fg 命令本身只对作业有意义。
    """
    pid = _resolve_job(job_id)
    if pid is None:
        out("fg: 无此作业", style="red")
        return 1

    info = jobs[pid]
    info["status"] = "running"
    _bump_job(pid)

    # 先抢终端，再 SIGCONT，防止恢复瞬间吃 SIGTTIN 又停
    _give_terminal_to(pid)
    try:
        os.killpg(pid, signal.SIGCONT)
    except OSError:
        pass

    try:
        try:
            _, status = os.waitpid(pid, os.WUNTRACED)
        except ChildProcessError:
            jobs.pop(pid, None)
            out(f"fg: 进程 {pid} 已不存在", style="yellow")
            return 1
    finally:
        _take_terminal_back()

    if os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
        jobs.pop(pid, None)
        return code

    if os.WIFSIGNALED(status):
        sig = os.WTERMSIG(status)
        jobs.pop(pid, None)
        return 128 + sig

    if os.WIFSTOPPED(status):
        sig = os.WSTOPSIG(status)
        info["status"] = "stopped"
        _bump_job(pid)
        out(f"[{info['jid']}]+ 已停止 (sig {sig})  {info['cmd']}", style="yellow")
        return 128 + sig

    return 0


def bg(job_id: str = "") -> int:
    pid = _resolve_job(job_id)
    if pid is None:
        out("bg: 无此作业", style="red")
        return 1

    info = jobs[pid]
    try:
        os.killpg(pid, signal.SIGCONT)
    except OSError as e:
        out(f"bg: {e}", style="red")
        return 1

    info["status"] = "background"
    _bump_job(pid)
    out(f"[{info['jid']}]+ {info['cmd']} &", style="yellow")
    return 0


def _parse_signal(sig: str) -> int | None:
    """
    兼容：
      "9" / "-9"          -> 9
      "TERM" / "SIGTERM"  -> SIGTERM
      "term" / "sigterm"  -> SIGTERM
    返回 None 表示未知信号。
    """
    sig_str = sig.lstrip("-").upper()

    if sig_str.isdigit():
        return int(sig_str)

    name = sig_str if sig_str.startswith("SIG") else "SIG" + sig_str
    signum = getattr(signal, name, None)
    return signum if isinstance(signum, signal.Signals) else None


def kill_job(job_id: str = "", sig: str = "TERM") -> int:
    """
    目标语义（贴 POSIX）：
      %N, %+, %-, %       -> 作业（killpg）
      裸数字              -> PID（kill）
    信号语义（宽松）：
      TERM / SIGTERM / term / 15 / -15 均可
    """
    if not job_id:
        out("kill: 需要作业号或 PID", style="red")
        return 1

    # ---------- 解析目标 ----------
    is_pg = False
    if job_id.startswith("%"):
        pid = _resolve_job(job_id)
        if pid is None:
            out(f"kill: 无此作业: {job_id}", style="red")
            return 1
        is_pg = True
    elif job_id.isdigit():
        pid = int(job_id)               # 裸数字 = PID
    else:
        out(f"kill: 无效目标: {job_id}", style="red")
        return 1

    # ---------- 解析信号 ----------
    signum = _parse_signal(sig)
    if signum is None:
        out(f"kill: 未知信号: {sig}", style="red")
        return 1

    # ---------- 发信号 ----------
    try:
        if is_pg:
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)
    except OSError as e:
        out(f"kill: {e}", style="red")
        return 1

    return 0


# ============================================================
# job_id -> pid 解析（fg/bg 用）
# ============================================================
def _resolve_job(job_id: str) -> int | None:
    """
    只解析作业引用：
      "" / "%" / "%+"   -> 最近作业
      "%-"              -> 前一个作业
      "%N"              -> 指定 jid
      "N"（裸数字）      -> 也按 jid 匹配（fg/bg 宽松）
    """
    if not jobs:
        return None

    if not job_id or job_id in ("%", "%+"):
        return max(jobs, key=lambda p: jobs[p]["seq"])

    if job_id == "%-":
        ordered = sorted(jobs, key=lambda p: jobs[p]["seq"])
        return ordered[-2] if len(ordered) >= 2 else None

    if job_id.startswith("%"):
        spec = job_id[1:]
        for pid, info in jobs.items():
            if str(info["jid"]) == spec:
                return pid
        return None

    if job_id.isdigit():
        for pid, info in jobs.items():
            if str(info["jid"]) == job_id:
                return pid

    return None