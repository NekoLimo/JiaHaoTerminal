"""pty 子进程管理。

替代 ``textual_terminal`` 里那个 494 行的 TerminalEmulator。设计要点，
每条都对应旧实现的一个具体缺陷：

1. **环境变量继承** ``os.environ``，只覆盖 TERM/COLORTERM。
   旧实现 ``env = dict(TERM=..., LC_ALL=..., HOME=...)`` 整个替换环境，
   导致子进程拿不到 PATH / PYTHONPATH / 代理变量。
2. **terminate() 全程非阻塞**：SIGTERM → 限时轮询 → SIGKILL。
   旧实现 ``os.waitpid(pid, 0)`` 会死等，子进程赖着不退就卡死 UI。
3. **幂等 + 异常安全**：进程已退出时调用不会抛 ProcessLookupError。
4. **TIOCSWINSZ 传完整 struct winsize**（4 个 unsigned short）。
   旧实现只 pack 2 个 short，长度不足。
5. **fd 确定性释放**，不留 ResourceWarning。
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import struct
import threading
import time

# ---------------------------------------------------------------------------
# 平台能力
# ---------------------------------------------------------------------------
# ★ 这两个模块在 Windows 上根本不存在，所以**不能**在顶层直接 import ——
#   那样连 ``import jiahao.widgets.pty`` 都会炸，整个 TUI 起不来，
#   用户看到的是 ImportError 而不是"这个平台还不支持"。
#
#   改成软导入：能导到就走原生路径，导不到就让 spawn() 抛一个说人话的错。
try:
    import fcntl
    import pty as _pty
    import termios
    HAVE_PTY = True
except ImportError:              # pragma: no cover - 只在 Windows 走到
    fcntl = None                 # type: ignore[assignment]
    _pty = None                  # type: ignore[assignment]
    termios = None               # type: ignore[assignment]
    HAVE_PTY = False

#: Windows 缺的常量：POSIX 才有 SIGKILL，那边是 TerminateProcess
_SIGKILL = getattr(signal, "SIGKILL", None)


class PtySpawnError(RuntimeError):
    """无法创建 pty 时抛出，附带可读的原因。"""


#: 平台不支持时的说明。写清楚**为什么**和**怎么办**，
#: 比一句 "not supported" 有用得多。
#: 子进程退出后，等这么久再关伪控制台 —— 让 conhost 把尾巴刷出来。
_REAP_DELAY = 0.35

NO_PTY_REASON = (
    "这个平台没有可用的伪终端。"
    "Windows 需要走 ConPTY（另一套 API，尚未实现）；"
    "Linux/macOS 请确认 pty/termios/fcntl 可用。"
)


#: 读 fd 时这些 errno 表示"对端已经没了"，应当作 EOF 处理。
#:
#: * ``EIO``       —— Linux 上子进程退出后读 pty master 的典型返回值
#: * ``EBADF``     —— fd 已被关闭
#: * ``ECONNRESET``—— 对端带着未读数据关闭（发的是 RST 而不是 FIN）
#: * ``EPIPE``     —— 管道断裂
#: * ``ENXIO``     —— 部分 Unix（BSD/macOS）用这个表示 pty EOF
#: * ``ESHUTDOWN`` —— socket 已 shutdown
_EOF_ERRNOS = frozenset(
    e for e in (
        getattr(errno, "EIO", None),
        getattr(errno, "EBADF", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ENXIO", None),
        getattr(errno, "ESHUTDOWN", None),
    )
    if e is not None
)


class PtyProcess:
    """在一个伪终端里跑子进程。

    两套实现，对外的接口完全一样：

    * **POSIX** —— ``pty.fork()``，master fd 可 select
    * **Windows** —— ConPTY + 两根管道句柄，就绪通知靠读线程
      （``ProactorEventLoop.add_reader`` 只支持 socket，管道不行）

    差异全在本类内部消化，调用方只管用 ``read``/``write``/``resize``/
    ``attach_reader``。
    """

    __slots__ = ("pid", "fd", "_closed", "_conpty", "_reader", "_phandle",
                 "_reaper")

    def __init__(self, pid: int, fd: int) -> None:
        self.pid = pid
        self.fd = fd
        self._closed = False
        #: Windows 分支用到的字段；POSIX 上恒为 None
        self._conpty = None
        self._reader = None
        self._phandle = None
        self._reaper = None

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------
    @classmethod
    def spawn(
        cls,
        argv: list[str],
        env: dict[str, str] | None = None,
        rows: int = 24,
        cols: int = 80,
        cwd: str | None = None,
    ) -> "PtyProcess":
        argv = list(argv)
        if not argv:
            raise ValueError("argv 不能为空")

        # 没有 fork/pty 时走 Windows 的 ConPTY 分支
        if not HAVE_PTY or not hasattr(os, "fork"):
            return cls._spawn_conpty(argv, env, rows, cols, cwd)

        # 关键修复 1：继承调用者环境，而不是凭空造一个
        child_env = dict(os.environ)
        child_env.setdefault("TERM", "xterm-256color")
        child_env["COLORTERM"] = "truecolor"
        if env:
            child_env.update(env)

        try:
            pid, fd = _pty.fork()
        except OSError as exc:  # pragma: no cover - 取决于环境
            raise PtySpawnError(
                f"无法分配 pty: {exc}。请检查 /dev/ptmx 权限与 devpts 挂载"
                "（容器里常见 ptmxmode=000 导致非 root 无法建 pty）。"
            ) from exc

        if pid == 0:
            # ---- 子进程：stdin/stdout/stderr 已接到 slave pty ----
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvpe(argv[0], argv, child_env)
            except FileNotFoundError:
                cls._child_die(127, f"{argv[0]}: command not found")
            except PermissionError:
                cls._child_die(126, f"{argv[0]}: permission denied")
            except OSError as exc:
                cls._child_die(126, f"{argv[0]}: {exc}")
            except BaseException as exc:  # noqa: BLE001 - 子进程必须兜住一切
                cls._child_die(1, f"{argv[0]}: {exc}")
            os._exit(127)  # 理论上到不了

        proc = cls(pid, fd)
        proc.resize(rows, cols)
        os.set_blocking(fd, False)
        return proc

    @staticmethod
    def _child_die(code: int, message: str) -> None:
        try:
            os.write(2, ("\r\n" + message + "\r\n").encode("utf-8", "replace"))
        except OSError:
            pass
        os._exit(code)

    @classmethod
    def _spawn_conpty(cls, argv, env, rows, cols, cwd) -> "PtyProcess":
        """Windows 分支：ConPTY + ``CreateProcessW``。

        环境变量的处理和 POSIX 分支保持一致，免得同一份命令在两个平台上
        行为不同（比如 ``TERM`` 缺失会让内层 shell 的输出缩水）。
        """
        from . import conpty

        if not conpty.AVAILABLE:            # pragma: no cover - 非 Windows
            raise PtySpawnError(NO_PTY_REASON)

        child_env = dict(os.environ)
        child_env.setdefault("TERM", "xterm-256color")
        child_env["COLORTERM"] = "truecolor"
        if env:
            child_env.update(env)

        try:
            pid, process_handle, pc = conpty.spawn(
                argv, env=child_env, rows=rows, cols=cols, cwd=cwd)
        except OSError as exc:
            raise PtySpawnError(f"无法创建 ConPTY: {exc}") from exc

        proc = cls(pid, -1)                 # Windows 上没有 fd 这个概念
        proc._conpty = pc
        proc._phandle = process_handle
        proc._reader = conpty.ConPtyReader(pc)
        proc._reaper = None
        proc._start_reaper()
        return proc

    def _start_reaper(self) -> None:
        """盯住子进程，它一退就**主动关掉伪控制台**。

        ★ Windows 特有的一步。不做的话：**在内嵌终端里敲 exit，TUI 不退。**

        POSIX 上子进程一退出，pty master 立刻返回 EIO，读端自然知道结束了。
        Windows 不是这样 —— 伪控制台（conhost）是个**独立对象**，子进程
        退出它不会自己关，输出管道一直开着，读线程的 ``ReadFile`` 就永远
        阻塞在那儿。于是：

            读不到 EOF → Terminal._pump 不发 Exited → TUI 一直等着

        所以要主动 ``WaitForSingleObject`` 等进程句柄 signal，再
        ``ClosePseudoConsole``。关掉之后 ``ReadFile`` 立刻返回
        ``ERROR_BROKEN_PIPE``，读线程把 ``eof`` 置上，链路就通了。

        ``_REAP_DELAY`` 是留给 conhost 把缓冲区里最后一点输出刷出来的时间。
        不等的话，shell 退出前打的告别信息可能被吞掉。
        """
        handle = self._phandle
        conpty_obj = self._conpty
        if handle is None or conpty_obj is None:
            return

        from . import conpty as _cp

        def _wait() -> None:
            try:
                _cp.KERNEL32.WaitForSingleObject(
                    _cp.HANDLE(handle), _cp.WAIT_INFINITE)
            except (OSError, AttributeError):
                return
            time.sleep(_REAP_DELAY)
            try:
                conpty_obj.close()
            except OSError:
                pass

        thread = threading.Thread(target=_wait, name="conpty-reaper", daemon=True)
        self._reaper = thread
        thread.start()

    # ------------------------------------------------------------------
    # 就绪通知
    # ------------------------------------------------------------------
    def attach_reader(self, loop, on_ready) -> None:
        """数据可能可读时调用 ``on_ready``（在**事件循环线程**里）。

        这一层抽象是 Windows 支持的关键：

        * POSIX —— ``loop.add_reader(fd)``，由 epoll/select 通知
        * Windows —— ``ProactorEventLoop.add_reader`` **只支持 socket**，
          管道句柄不行。只能起一个线程阻塞在 ``ReadFile`` 上，
          读到东西再 ``call_soon_threadsafe`` 把回调甩回事件循环。
        """
        if self._reader is not None:
            self._reader.start(loop, on_ready)
            return
        if self.fd is not None and self.fd >= 0:
            loop.add_reader(self.fd, on_ready)

    def detach_reader(self, loop) -> None:
        """撤销 ``attach_reader``。可重复调用。"""
        if self._reader is not None:
            self._reader.stop()
            return
        if self.fd is not None and self.fd >= 0:
            try:
                loop.remove_reader(self.fd)
            except (OSError, ValueError):
                pass

    @classmethod
    def from_fd(cls, fd: int, pid: int = -1) -> "PtyProcess":
        """用已有 fd 构造。

        测试用：可以传 ``socket.socketpair()`` 的一端，从而在**无法创建 pty**
        的环境里覆盖读取/断开路径。
        """
        proc = cls(pid, fd)
        os.set_blocking(fd, False)
        return proc

    # ------------------------------------------------------------------
    # 窗口尺寸
    # ------------------------------------------------------------------
    def resize(self, rows: int, cols: int) -> None:
        """关键修复 4：发送完整的 ``struct winsize``。"""
        if self._closed:
            return
        rows = max(1, int(rows))
        cols = max(1, int(cols))

        if self._conpty is not None:        # Windows
            self._conpty.resize(rows, cols)
            return

        try:
            fcntl.ioctl(
                self.fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def read(self, size: int = 65536) -> bytes | None:
        """读一次。

        * 有数据 → ``bytes``
        * 暂无数据 → ``b""``（非阻塞，不是错误）
        * 对端已关闭 → ``None``
        """
        if self._closed:
            return None
        if self._conpty is not None:        # Windows：阻塞读，只给读线程用
            return self._conpty.read(size)
        try:
            data = os.read(self.fd, size)
        except BlockingIOError:
            return b""
        except InterruptedError:
            return b""
        except OSError as exc:
            # 对端消失的不同表现形式统一归为 EOF
            if exc.errno in _EOF_ERRNOS:
                return None
            return b""
        return data if data else None

    def read_all(self, limit: int | None = 1 << 20) -> bytes | None:
        """把当前缓冲区一次读干（非阻塞）。

        ``None`` 表示对端已关闭。``limit`` 防止无限刷屏时饿死事件循环。
        """
        # Windows：数据是读线程攒的，直接取缓冲区。
        # 注意"暂时没数据"和"已 EOF"要分开 —— 前者返回 b""，
        # 后者返回 None；混了会让终端刚启动就以为自己退出了。
        if self._reader is not None:
            data = self._reader.drain(limit or (1 << 20))
            if not data and self._reader.eof:
                return None
            return data

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = self.read()
            if chunk is None:
                return None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if limit is not None and total >= limit:
                break
        return b"".join(chunks)

    def write(self, data: str | bytes) -> bool:
        if self._closed:
            return False
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if self._conpty is not None:        # Windows
            return self._conpty.write(data) > 0
        try:
            os.write(self.fd, data)
            return True
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def is_alive(self) -> bool:
        if self.pid is None or self.pid <= 0:
            return not self._closed
        if self._phandle is not None:       # Windows
            return self._win_alive()
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return False
        return done == 0

    def _win_alive(self) -> bool:
        """Windows：进程句柄还在、退出码还是 STILL_ACTIVE 就是活着。"""
        try:
            from . import conpty
            if conpty.KERNEL32 is None:     # pragma: no cover - 非 Windows
                return False
            code = conpty.DWORD(0)
            if not conpty.KERNEL32.GetExitCodeProcess(
                    conpty.HANDLE(self._phandle), ctypes.byref(code)):
                return False
            return code.value == conpty.STILL_ACTIVE
        except Exception:                   # noqa: BLE001 - 查不到就当死了
            return False

    def exit_code_windows(self, timeout: float = 0.2) -> int:
        """Windows 上取退出码（没有 waitpid，只能问进程句柄）。

        先等一下再问：子进程刚结束的瞬间退出码可能还是 STILL_ACTIVE。
        """
        if not self._phandle:
            return 0
        try:
            from . import conpty
            if conpty.KERNEL32 is None:     # pragma: no cover
                return 0
            handle = conpty.HANDLE(self._phandle)
            conpty.KERNEL32.WaitForSingleObject(
                handle, int(max(0.0, timeout) * 1000))
            code = conpty.DWORD(0)
            if not conpty.KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return 0
            value = int(code.value)
            return 0 if value == conpty.STILL_ACTIVE else value
        except Exception:                   # noqa: BLE001
            return 0

    @staticmethod
    def _reap(pid: int) -> bool:
        """非阻塞回收。返回 True 表示进程已经没了。"""
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        except OSError:
            return True
        return done == pid

    def _signal(self, sig: int) -> None:
        # pty.fork() 里子进程 setsid 过，pgid == pid，所以整组一起发
        try:
            os.killpg(self.pid, sig)
            return
        except OSError:
            pass
        try:
            os.kill(self.pid, sig)
        except OSError:
            pass

    def terminate(self, grace: float = 0.5) -> None:
        """关键修复 2/3：非阻塞、幂等的优雅终止。

        SIGTERM → 在 ``grace`` 内轮询回收 → 仍在则 SIGKILL → 再限时回收。
        任何一步失败都不抛异常。
        """
        # Windows：没有信号，直接 TerminateProcess + 关 ConPTY
        if self._conpty is not None:
            self._terminate_windows()
            return

        if self.pid is None or self.pid <= 0:
            return
        pid = self.pid

        self._signal(signal.SIGTERM)

        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if self._reap(pid):
                return
            time.sleep(0.01)

        if _SIGKILL is None:                  # pragma: no cover - Windows
            return
        self._signal(_SIGKILL)

        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if self._reap(pid):
                return
            time.sleep(0.01)
        # 超时也不再等 —— 绝不阻塞 UI

    def _terminate_windows(self, grace: float = 0.5) -> None:
        """Windows 的终止路径。

        顺序有讲究：**先关 ConPTY**，它会通知挂在上面还没退出的进程；
        隔一小会儿还没退再 ``TerminateProcess`` 兜底。

        ``TerminateProcess`` 只杀直接子进程，孙进程会漏 —— 彻底解决要用
        Job Object，这里没上，留个已知限制。
        """
        try:
            from . import conpty
            if conpty.KERNEL32 is None:     # pragma: no cover
                return

            self._conpty.close()

            if not self._phandle:
                return
            deadline = time.monotonic() + max(0.0, grace)
            while time.monotonic() < deadline:
                if not self._win_alive():
                    return
                time.sleep(0.01)
            conpty.KERNEL32.TerminateProcess(
                conpty.HANDLE(self._phandle), 1)
        except Exception:                   # noqa: BLE001 - 终止失败不该抛
            pass

    def close(self) -> None:
        """关掉 fd / ConPTY。可重复调用。"""
        if self._closed:
            return
        self._closed = True

        # ★ 顺序有讲究：**先关 ConPTY，再停读线程**。
        #
        # 读线程阻塞在 ReadFile 上，stop() 只是设个标志位，
        # 打断不了已经在内核里等着的读。先关掉句柄，ReadFile 才会返回，
        # 线程才能退出。反过来写就永远停在 join 超时上（自检程序抓到的）。
        if self._conpty is not None:
            self._conpty.close()
        if self._reader is not None:
            self._reader.stop()
        if self._phandle:                   # Windows 的进程句柄也要收
            try:
                from . import conpty
                if conpty.KERNEL32 is not None:
                    conpty.KERNEL32.CloseHandle(conpty.HANDLE(self._phandle))
            except Exception:               # noqa: BLE001
                pass
            self._phandle = None

        if self.fd is not None and self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass

    def shutdown(self, grace: float = 0.5) -> None:
        """terminate + close，顺序安全。"""
        try:
            self.terminate(grace=grace)
        finally:
            self.close()

    # ------------------------------------------------------------------
    def __enter__(self) -> "PtyProcess":
        return self

    def __exit__(self, *_exc) -> None:
        self.shutdown()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        state = "closed" if self._closed else "open"
        return f"<PtyProcess pid={self.pid} fd={self.fd} {state}>"
