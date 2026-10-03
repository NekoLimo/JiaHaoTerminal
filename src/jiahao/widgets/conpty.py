"""Windows 伪终端（ConPTY）。

Windows 10 1809 起提供了 ConPTY，但接口和 POSIX 的 pty **完全不同**：

===================  ==========================  ============================
                     POSIX                       Windows / ConPTY
===================  ==========================  ============================
创建                 ``pty.fork()``              ``CreatePseudoConsole``
接进程               ``execvp`` 自动继承         ``CreateProcessW`` + 属性表
读写                 master fd（可 select）      两根管道句柄（``ReadFile``）
改尺寸               ``TIOCSWINSZ``              ``ResizePseudoConsole``
结束                 ``SIGTERM``/``SIGKILL``     ``TerminateProcess``
===================  ==========================  ============================

## 关于可测性

作者在 Linux 上开发，**没有 Windows 机器**。所以这里刻意做了两件事：

1. **结构体用定宽 ctypes 类型定义**，不用 ``ctypes.wintypes``。
   后者的 ``DWORD`` 是 ``c_ulong`` —— Linux 上 8 字节、Windows 上 4 字节
   （LP64 vs LLP64），布局随平台漂移，**在 Linux 上根本算不出正确尺寸**，
   而这个错误在 Windows 上又不会暴露。定宽之后尺寸在哪都能算，
   测试里直接对着 Win32 文档的值断言（104 / 112 / 24）。

2. **把 Win32 调用和数据布局分开**。布局是纯计算，可以在这里验证；
   真正的 API 调用只能在 Windows 上跑，测试用假的 DLL 桩验证调用序列。

所以：**结构布局与调用序列有测试覆盖；真实行为未经实机验证。**

## 两个踩过的坑

**① ``int(ctypes 标量)`` 会抛异常。** Python 3.12 上
``int(ctypes.c_uint32(5))`` 报 ``ValueError: invalid literal for int()`` ——
它把 ctypes 对象当字符串解析了。**一律用 ``.value``**。

**② 别用 ``ctypes.wintypes``。** 见上面第 1 条。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time

__all__ = [
    "AVAILABLE", "REASON", "COORD", "STARTUPINFOW", "STARTUPINFOEXW",
    "PROCESS_INFORMATION", "ConPty", "spawn", "LAST_ERROR_UNAVAILABLE",
]

#: ConPTY 是不是能用（Windows 10 1809+）
AVAILABLE = sys.platform.startswith("win")
REASON = "" if AVAILABLE else "ConPTY 只在 Windows 上可用"

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
#: 属性表里表示"把伪控制台挂到这个进程上"。值是 Win32 头文件里的固定常量。
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100

#: ``(HANDLE)(intptr_t)-1``
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
#: 最常见的 HRESULT 失败码
LAST_ERROR_UNAVAILABLE = "ConPTY 不可用（需要 Windows 10 1809 及以上）"

# ---------------------------------------------------------------------------
# 定宽类型
# ---------------------------------------------------------------------------
# ★ 显式定宽，不用 ctypes.wintypes —— 理由见模块开头。
#   Windows 是 LLP64（long 4 字节），Linux/macOS 是 LP64（long 8 字节），
#   拿 wintypes.DWORD 定义结构体会让布局随平台变。
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BOOL = ctypes.c_int32
HANDLE = ctypes.c_void_p
LPWSTR = ctypes.c_wchar_p
HRESULT = ctypes.c_long


class COORD(ctypes.Structure):
    """控制台尺寸，单位是字符。注意是**短整型**，不是 DWORD。"""

    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", LPWSTR),
        ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR),
        ("dwX", DWORD), ("dwY", DWORD),
        ("dwXSize", DWORD), ("dwYSize", DWORD),
        ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
        ("wShowWindow", WORD), ("cbReserved2", WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    """比 ``STARTUPINFOW`` 多一个属性表指针。

    ``CreateProcessW`` 认这个结构靠 ``dwFlags`` 里的
    ``EXTENDED_STARTUPINFO_PRESENT``，同时 ``cb`` 要填**本结构**的大小。
    """

    _fields_ = [("StartupInfo", STARTUPINFOW),
                ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", HANDLE), ("hThread", HANDLE),
        ("dwProcessId", DWORD), ("dwThreadId", DWORD),
    ]


# ---------------------------------------------------------------------------
# kernel32 函数声明
# ---------------------------------------------------------------------------
def _load_kernel32():
    """加载 kernel32 并把签名声明好。

    在非 Windows 上返回 None —— 模块仍然可以 import，
    结构体定义也还能用来做布局测试。
    """
    if not AVAILABLE:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(HANDLE), ctypes.POINTER(HANDLE),
        ctypes.c_void_p, DWORD,
    ]
    kernel32.CreatePipe.restype = BOOL

    # HRESULT CreatePseudoConsole(COORD, HANDLE in, HANDLE out, DWORD, HPCON*)
    kernel32.CreatePseudoConsole.argtypes = [
        COORD, HANDLE, HANDLE, DWORD, ctypes.POINTER(HANDLE),
    ]
    kernel32.CreatePseudoConsole.restype = HRESULT

    kernel32.ResizePseudoConsole.argtypes = [HANDLE, COORD]
    kernel32.ResizePseudoConsole.restype = HRESULT

    kernel32.ClosePseudoConsole.argtypes = [HANDLE]
    kernel32.ClosePseudoConsole.restype = None

    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, DWORD, DWORD, ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = BOOL

    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = BOOL

    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None

    kernel32.CreateProcessW.argtypes = [
        LPWSTR, LPWSTR, ctypes.c_void_p, ctypes.c_void_p, BOOL, DWORD,
        ctypes.c_void_p, LPWSTR, ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = BOOL

    kernel32.ReadFile.argtypes = [
        HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = BOOL

    kernel32.WriteFile.argtypes = [
        HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = BOOL

    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.CloseHandle.restype = BOOL

    kernel32.TerminateProcess.argtypes = [HANDLE, ctypes.c_uint]
    kernel32.TerminateProcess.restype = BOOL

    kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    kernel32.GetExitCodeProcess.restype = BOOL

    kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    kernel32.WaitForSingleObject.restype = DWORD

    return kernel32


KERNEL32 = _load_kernel32()

#: ``ctypes.get_last_error`` 和 ``WinDLL`` 一样是 Windows 专有的。
#: 取不到就给个返回 0 的替身 —— 非 Windows 上误调到 ``_check`` 也不该崩。
_get_last_error = getattr(ctypes, "get_last_error", lambda: 0)

#: ``WaitForSingleObject`` 的返回码
WAIT_OBJECT_0 = 0x00000000
WAIT_INFINITE = 0xFFFFFFFF
WAIT_TIMEOUT = 0x00000102
STILL_ACTIVE = 259


def _check(ok, what: str) -> None:
    """Win32 里 ``BOOL`` 返回 0 表示失败，错误码在 ``GetLastError``。"""
    if ok:
        return
    err = _get_last_error()
    raise OSError(0, f"{what} 失败 (GetLastError={err})")


def _check_hresult(hr: int, what: str) -> None:
    """``HRESULT`` 小于 0 表示失败。"""
    if hr >= 0:
        return
    raise OSError(0, f"{what} 失败 (HRESULT=0x{hr & 0xFFFFFFFF:08X})")


# ---------------------------------------------------------------------------
# 进程属性表
# ---------------------------------------------------------------------------
class _AttributeList:
    """``PROC_THREAD_ATTRIBUTE_LIST`` 的 RAII 包装。

    用法固定三步，少一步 ``CreateProcessW`` 就会失败：
    先问需要多大，再真的初始化，用完必须 ``DeleteProcThreadAttributeList``。
    """

    def __init__(self, count: int = 1) -> None:
        size = ctypes.c_size_t(0)
        # 第一次调用故意传 NULL：只为问出需要多少字节，必然"失败"
        KERNEL32.InitializeProcThreadAttributeList(
            None, count, 0, ctypes.byref(size))
        if size.value == 0:
            raise OSError(0, "InitializeProcThreadAttributeList 没给出所需大小")

        self._buf = ctypes.create_string_buffer(size.value)
        self._ptr = ctypes.cast(self._buf, ctypes.c_void_p)
        _check(KERNEL32.InitializeProcThreadAttributeList(
            self._ptr, count, 0, ctypes.byref(size)),
            "InitializeProcThreadAttributeList")
        self._alive = True

    def set_pseudoconsole(self, hpc: int) -> None:
        r"""把 ConPTY 句柄挂进属性表。

        ★ **``lpValue`` 传的是 ``hpc`` 本身，不是它的地址。**

        这一条和文档、甚至和微软开发者自己的说法都相反，但必须这么写。

        ``HPCON`` 是个 ``void *``，指向一个 3 元素的句柄数组（signal pipe、
        ``\\Device\\ConDrv\\Reference``、conhost 进程）。内核处理这个属性时
        **把 ``lpValue`` 直接当 HPCON 用**，再解引用它。

        见 https://github.com/microsoft/terminal/issues/6705 ——

        > All we're doing is unpacking the first member from the HPCON ...
        > It should have been ``&hpc, sizeof(hpc)``.
        > ... it is now a compatibility concern to change ☹️

        微软承认写错了，但**改不了**（兼容性）。所以只能按实际实现来。

        ★ 传 ``&hpc`` 的后果非常隐蔽：``UpdateProcThreadAttribute`` 不校验、
        ``CreateProcessW`` 照样成功、进程真的起来了 —— 但内核把**栈地址**
        当 HPCON 解引用，子进程连不上伪控制台，于是**永远读不到一个字节**。

        这个 bug 就是靠"在真 Windows 上跑一遍自检"抓出来的：
        结构体布局、句柄创建、CreateProcess 全部 PASS，数据 0 字节。
        """
        value = ctypes.c_void_p(hpc)
        _check(KERNEL32.UpdateProcThreadAttribute(
            self._ptr, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            value,                              # ← 直接传句柄值，不要 &value
            ctypes.sizeof(HANDLE), None, None),
            "UpdateProcThreadAttribute(PSEUDOCONSOLE)")

    def close(self) -> None:
        if self._alive:
            KERNEL32.DeleteProcThreadAttributeList(self._ptr)
            self._alive = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _env_block(env: dict[str, str]):
    """``CREATE_UNICODE_ENVIRONMENT`` 要的环境块：``k=v\\0k=v\\0\\0``。"""
    text = "\0".join(f"{k}={v}" for k, v in sorted(env.items()))
    # 末尾补一个 \0，create_unicode_buffer 自己再加一个 —— 正好双 \0 结尾
    return ctypes.create_unicode_buffer(text + "\0")


# ---------------------------------------------------------------------------
# ConPTY 实例
# ---------------------------------------------------------------------------
class ConPty:
    """一个 ConPTY 实例和它的两根管道。

    数据流向::

        ConPty.hpc ──写出──> write_handle ──> [ConPTY] ──> 子进程 stdin
        ConPty.hpc <──读入── read_handle  <── [ConPTY] <── 子进程 stdout

    ``hpc`` 归本对象所有；``read_handle`` / ``write_handle`` 也是，
    ``close()`` 会一起收掉。
    """

    __slots__ = ("hpc", "read_handle", "write_handle", "_closed")

    def __init__(self, hpc: int, read_handle: int, write_handle: int) -> None:
        self.hpc = hpc
        self.read_handle = read_handle
        self.write_handle = write_handle
        self._closed = False

    def resize(self, rows: int, cols: int) -> bool:
        """改尺寸。失败返回 False（不改尺寸不该让终端整个挂掉）。"""
        if self._closed or KERNEL32 is None:
            return False
        try:
            _check_hresult(
                KERNEL32.ResizePseudoConsole(
                    HANDLE(self.hpc), COORD(int(cols), int(rows))),
                "ResizePseudoConsole")
            return True
        except OSError:
            return False

    def write(self, data: bytes) -> int:
        """往 ConPTY 写数据（就是喂给子进程 stdin）。"""
        if self._closed or KERNEL32 is None or not data:
            return 0
        written = DWORD(0)
        buf = ctypes.create_string_buffer(data, len(data))
        ok = KERNEL32.WriteFile(HANDLE(self.write_handle), buf, DWORD(len(data)),
                                ctypes.byref(written), None)
        return int(written.value) if ok else 0

    def read(self, size: int = 65536) -> bytes | None:
        """从 ConPTY 读一块。

        返回 ``None`` 表示管道断了（子进程结束、ConPTY 关了）。
        **这是阻塞调用** —— 调用方要放在读线程里，别放 UI 线程。
        """
        if self._closed or KERNEL32 is None:
            return None
        buf = ctypes.create_string_buffer(size)
        got = DWORD(0)
        ok = KERNEL32.ReadFile(HANDLE(self.read_handle), buf, DWORD(size),
                               ctypes.byref(got), None)
        if not ok:
            # ERROR_BROKEN_PIPE(109) / ERROR_HANDLE_EOF(38) 都算正常结束
            err = _get_last_error()
            if err in (109, 38, 6):        # BROKEN_PIPE / HANDLE_EOF / INVALID_HANDLE
                return None
            return None
        if got.value == 0:
            return None
        return buf.raw[:got.value]

    def close(self) -> None:
        """关掉 ConPTY 和两根管道。

        ``ClosePseudoConsole`` 会让挂在上面还没退出的进程收到关闭信号，
        所以顺序是：先关 PC，再关句柄。
        """
        if self._closed or KERNEL32 is None:
            self._closed = True
            return
        self._closed = True
        try:
            if self.hpc:
                KERNEL32.ClosePseudoConsole(HANDLE(self.hpc))
        except OSError:
            pass
        for handle in (self.read_handle, self.write_handle):
            if handle and handle != INVALID_HANDLE_VALUE:
                try:
                    KERNEL32.CloseHandle(HANDLE(handle))
                except OSError:
                    pass
        self.hpc = self.read_handle = self.write_handle = 0

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ConPty hpc={self.hpc} r={self.read_handle} w={self.write_handle}>"


def spawn(argv, env=None, rows: int = 24, cols: int = 80, cwd=None):
    """起一个挂着 ConPTY 的进程。

    返回 ``(pid, process_handle, conpty)``。

    失败时抛 ``OSError``，并且**把已经拿到的句柄都关掉** ——
    句柄泄漏在 Windows 上比在 Unix 上更麻烦，进程不退就一直是占用。
    """
    if not AVAILABLE:
        raise OSError(0, REASON)
    if not argv:
        raise ValueError("argv 不能为空")

    cmdline = subprocess.list2cmdline([str(a) for a in argv])
    child_env = dict(os.environ if env is None else env)
    env_block = _env_block(child_env)

    in_read = HANDLE()
    in_write = HANDLE()
    out_read = HANDLE()
    out_write = HANDLE()
    hpc = HANDLE()
    pi = PROCESS_INFORMATION()
    attrs = None

    try:
        # 两根匿名管道。**不能**设成可继承：ConPTY 自己持有对端，
        # 子进程不需要看到它们（子进程只通过伪控制台通信）。
        _check(KERNEL32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write),
                                   None, 0), "CreatePipe(stdin)")
        _check(KERNEL32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write),
                                   None, 0), "CreatePipe(stdout)")

        # ConPTY 要的是「输入的读端」和「输出的写端」
        _check_hresult(KERNEL32.CreatePseudoConsole(
            COORD(int(cols), int(rows)), in_read, out_write, 0,
            ctypes.byref(hpc)), "CreatePseudoConsole")
        if not hpc.value:
            raise OSError(0, LAST_ERROR_UNAVAILABLE)

        # 这两端从此归 ConPTY 所有，我们立刻放手；
        # 留着不关会让管道永远不出现"写端全关"的 EOF 状态。
        KERNEL32.CloseHandle(in_read)
        in_read = None
        KERNEL32.CloseHandle(out_write)
        out_write = None
        # 留着的是 in_write（喂子进程 stdin）和 out_read（读子进程输出）

        attrs = _AttributeList(1)
        attrs.set_pseudoconsole(hpc.value)

        # ★★ 不要设 STARTF_USESTDHANDLES，也不要碰 hStd*。
        #
        # 子进程的 stdin/stdout/stderr 由**控制台子系统**接管 —— 挂了
        # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE 之后，Windows 会把它们接到
        # 伪控制台上。自己指定反而坏事。
        #
        # 踩过的坑：本来写成 STARTF_USESTDHANDLES + 三个 INVALID_HANDLE_VALUE
        # （"反正真正的 stdio 由 ConPTY 接管"），结果 Python 子进程起来一看
        # stdin 句柄是无效的，直接把 sys.stdin 设成 None，于是
        #     RuntimeError: input(): lost sys.stdin
        # 交互式 shell 一调用 input() 就闪退。
        #
        # 阴的地方在于 **stdout 是好的** —— 只 print 不读 stdin 的程序
        # （比如自检里的 --child 模式）完全测不出来。
        #
        # 微软官方 EchoCon 示例就是一个全零的 STARTUPINFOEX，只填 cb：
        #     STARTUPINFOEX startupInfo{};
        #     startupInfo.StartupInfo.cb = sizeof(STARTUPINFOEX);
        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        # dwFlags = 0，hStd* = NULL（create_string_buffer 之外全零初始化）
        si.lpAttributeList = ctypes.cast(attrs._ptr, ctypes.c_void_p)

        flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
        _check(KERNEL32.CreateProcessW(
            None, ctypes.create_unicode_buffer(cmdline),
            None, None, False, flags,
            ctypes.cast(env_block, ctypes.c_void_p),
            ctypes.c_wchar_p(cwd) if cwd else None,
            ctypes.byref(si.StartupInfo), ctypes.byref(pi)),
            "CreateProcessW")

        return (int(pi.dwProcessId), int(pi.hProcess),
                ConPty(hpc.value, out_read.value, in_write.value))

    except BaseException:
        # 失败路径：把已经拿到的句柄全关掉再抛，别泄漏
        for handle in (in_read, in_write, out_read, out_write, hpc):
            if handle:
                try:
                    KERNEL32.CloseHandle(handle)
                except OSError:
                    pass
        if pi.hProcess:
            KERNEL32.CloseHandle(pi.hProcess)
        if pi.hThread:
            KERNEL32.CloseHandle(pi.hThread)
        raise
    finally:
        if attrs is not None:
            attrs.close()


class ConPtyReader:
    """在读线程里阻塞读 ConPTY，把数据攒进缓冲区。

    为什么需要它：``ProactorEventLoop`` 的 ``add_reader`` **只支持 socket**，
    管道句柄不行。所以 Windows 上不能用 ``loop.add_reader``，
    只能自己起一个线程阻塞在 ``ReadFile`` 上，读到东西再
    ``loop.call_soon_threadsafe`` 通知事件循环。

    POSIX 那边是 select/epoll 直接等 fd，不需要线程。
    """

    def __init__(self, conpty: ConPty, chunk: int = 65536) -> None:
        self._conpty = conpty
        self._chunk = chunk
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._eof = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def eof(self) -> bool:
        return self._eof

    def start(self, loop, on_ready) -> None:
        """起读线程。``on_ready`` 会在**事件循环线程**里被调用。"""

        def body() -> None:
            while not self._stop.is_set():
                data = self._conpty.read(self._chunk)
                if data is None:
                    self._eof = True
                    break
                if not data:
                    continue
                with self._lock:
                    self._buf += data
                try:
                    loop.call_soon_threadsafe(on_ready)
                except RuntimeError:
                    # 事件循环已经关了，收工
                    break
            self._eof = True
            try:
                loop.call_soon_threadsafe(on_ready)
            except RuntimeError:
                pass

        self._thread = threading.Thread(target=body, daemon=True,
                                        name="conpty-reader")
        self._thread.start()

    def drain(self, limit: int = 1 << 20) -> bytes:
        """取走缓冲区里的数据。返回空表示暂时没有，``eof`` 为真表示结束。"""
        with self._lock:
            if not self._buf:
                return b""
            data = bytes(self._buf[:limit])
            del self._buf[:limit]
            return data

    def stop(self, timeout: float = 0.5) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
