"""基于 pyte 的 Textual 终端组件。

替代 ``textual_terminal.Terminal``。与旧实现的差异：

============  ==========================================  ============================
方面            旧 (textual-terminal 0.3.0)                 新
============  ==========================================  ============================
子进程环境      ``env`` 整个替换，丢掉 PATH/PYTHONPATH      继承 ``os.environ``，只覆盖 TERM
UTF-8 解码      每块 ``.decode()``，跨包边界丢字符          ``pyte.ByteStream`` 增量解码
渲染            每块数据全屏重建 4800 个单元格               脏行缓存 + 区段化样式 + 尾部裁剪
刷新频率         每块数据一次 refresh                        合并到最多 60fps
读取            事件回调里阻塞 ``read(65536)``              非阻塞 ``read_all()``
退出             ``waitpid(pid, 0)`` 死等                    限时轮询 → SIGKILL，绝不阻塞
winsize         pack 2 个 short（长度不足）                  pack 完整 ``struct winsize``
样式映射         比较 8 个属性但只用 ``bold``                9 个属性全映射
fd              泄漏（源码 FIXME 自认）                      确定性关闭
============  ==========================================  ============================
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time

from textual import events
from textual.message import Message
from textual.widget import Widget

from ..utils.compat import split_command
from .emulator import TerminalEmulator
from .pty import PtyProcess, PtySpawnError, HAVE_PTY
from ..utils import anim

__all__ = ["Terminal", "TerminalExited"]

#: 渲染节流：最多 60fps。输出再猛也不会把 UI 拖垮。
FRAME_SECONDS = 1.0 / 60.0

#: "主动渐变"的动画帧率。每帧要整屏重建，比普通刷新贵，
#: 20fps 已经足够顺滑，CPU 占用也可接受。
ANIM_FPS = 20
ANIM_SECONDS = 1.0 / ANIM_FPS

#: 一次事件循环里最多读多少字节，防止无限刷屏饿死 UI
_READ_LIMIT = 1 << 20


def _encode_key(event: events.Key) -> str | None:
    """把 Textual 按键事件编成终端字节序列。"""
    key = event.key

    special = _SPECIAL_KEYS.get(key)
    if special is not None:
        return special

    # ctrl+<字母> → 0x01..0x1a
    if key.startswith("ctrl+"):
        rest = key[5:]
        if len(rest) == 1:
            low = rest.lower()
            if "a" <= low <= "z":
                return chr(ord(low) - 0x60)
            if rest == "@":
                return "\x00"
            if rest in ("[", "3"):
                return "\x1b"
            if rest in ("\\", "4"):
                return "\x1c"
            if rest in ("]", "5"):
                return "\x1d"
            if rest in ("^", "6"):
                return "\x1e"
            if rest in ("_", "7", "/"):
                return "\x1f"
            if rest in ("?", "8"):
                return "\x7f"
        return None

    if key.startswith("alt+") and event.character:
        return "\x1b" + event.character

    if event.character:
        return event.character
    return None


# 光标键用 CSI 形式（``\x1b[A``）而不是旧实现的 SS3（``\x1bOA``）：
# SS3 只在 application-cursor-keys 模式下才对，readline 两种都认，
# 但 CSI 在更多程序里更保险。
_SPECIAL_KEYS: dict[str, str] = {
    "enter": "\r",
    "return": "\r",
    "tab": "\t",
    "shift+tab": "\x1b[Z",
    "escape": "\x1b",
    "backspace": "\x7f",
    "delete": "\x1b[3~",
    "insert": "\x1b[2~",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
    "f12": "\x1b[24~",
}


class Terminal(Widget, can_focus=True):
    """在 Textual 里内嵌一个真实终端。"""

    DEFAULT_CSS = """
    Terminal {
        width: 100%;
        height: 100%;
        background: $surface;
    }
    """

    class Exited(Message):
        """子进程结束。"""

        def __init__(self, terminal: "Terminal", returncode: int | None) -> None:
            self.terminal = terminal
            self.returncode = returncode
            super().__init__()

    def __init__(
        self,
        command: str,
        env: dict[str, str] | None = None,
        *,
        release_focus_key: str = "ctrl+f1",
        autostart: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self.command = command
        self._env = env
        self._release_focus_key = release_focus_key
        self._autostart = autostart

        self._proc: PtyProcess | None = None
        self._pump_task: asyncio.Task | None = None
        self._anim_task: asyncio.Task | None = None
        self._gradient_rev_seen = 0
        self._emulator = TerminalEmulator(80, 24)
        self._spawn_error: str | None = None
        self._exit_code: int | None = None

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def emulator(self) -> TerminalEmulator:
        return self._emulator

    @property
    def process(self) -> PtyProcess | None:
        return self._proc

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._exit_code is None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_mount(self) -> None:
        self.focus()
        if self._autostart:
            self.start()

    async def on_unmount(self) -> None:
        await self._teardown()

    def start(self) -> None:
        """启动子进程。幂等：已在运行则什么都不做。"""
        if self._proc is not None:
            return

        size = self.size
        rows = size.height if size.height > 1 else 24
        cols = size.width if size.width > 2 else 80

        try:
            # ★ 按平台拆：Windows 上不能用 POSIX 规则（反斜杠会被当转义符）
            argv = split_command(self.command)
            if not argv:
                raise ValueError("命令为空")
            self._proc = self._spawn_process(argv, rows, cols)
        except (PtySpawnError, ValueError, OSError) as exc:
            # 起不来也别炸掉整个 App —— 把原因画在终端区域里
            self._spawn_error = str(exc)
            self._emulator.feed(
                f"\r\n  [终端启动失败] {exc}\r\n".encode("utf-8", "replace")
            )
            self.refresh()
            return

        self._emulator.resize(rows, cols)
        self._pump_task = asyncio.create_task(self._pump())

    def _spawn_process(self, argv: list[str], rows: int, cols: int) -> PtyProcess:
        """创建子进程。

        单独抽出来是**测试接缝**：测试可以覆盖它，注入一个用
        ``socketpair`` 支撑的假进程，从而在没有 pty 的环境里完整验证
        数据泵、按键回写、EOF 处理与节流刷新。
        """
        return PtyProcess.spawn(argv, env=self._env, rows=rows, cols=cols)

    async def _teardown(self) -> None:
        anim, self._anim_task = self._anim_task, None
        if anim is not None:
            anim.cancel()
            try:
                await anim
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        task, self._pump_task = self._pump_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        proc, self._proc = self._proc, None
        if proc is not None:
            # terminate() 自身限时且幂等，这里不会卡
            await asyncio.get_running_loop().run_in_executor(
                None, proc.shutdown
            )

    def stop(self) -> None:
        """同步请求停止（给非 async 场景用）。"""
        if self._anim_task is not None:
            self._anim_task.cancel()
            self._anim_task = None
        if self._pump_task is not None:
            self._pump_task.cancel()
            self._pump_task = None
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.shutdown()

    # ------------------------------------------------------------------
    # 主动渐变（整屏色相随时间流动）
    # ------------------------------------------------------------------
    def _sync_animation(self) -> None:
        """按 emulator 的渐变状态开关动画任务。"""
        mode = self._emulator.gradient_mode
        # 动画总开关关掉时不跑（--no-con）。整屏 20fps 重绘在
        # 测试环境里纯属浪费，而且会让输出不稳定。
        want = mode in ("anim", "both") and anim.animations_enabled()

        if want and self._anim_task is None:
            self._anim_task = asyncio.create_task(self._gradient_tick())
        elif not want and self._anim_task is not None:
            self._anim_task.cancel()
            self._anim_task = None

        if not want and self._emulator.hue_shift:
            # 关掉动画后必须复位色相并重绘，否则画面会停在最后一帧
            self._emulator.hue_shift = 0.0
            self.refresh()

    async def _gradient_tick(self) -> None:
        """按 ANIM_FPS 持续推进色相偏移并重绘。

        注意不要把这个方法叫 ``_animate``：Textual 的 ``Styles`` 上已经有一个
        ``self._animate``（CSS 动画绑定，默认 None），同名会被实例属性遮蔽，
        调用时直接 TypeError。
        """
        try:
            while True:
                await asyncio.sleep(ANIM_SECONDS)
                mode = self._emulator.gradient_mode
                if mode not in ("anim", "both"):
                    break
                speed = self._emulator.gradient_speed
                if speed <= 0:
                    break
                self._emulator.hue_shift = (time.monotonic() * speed) % 1.0
                self.refresh()
        except asyncio.CancelledError:
            raise
        finally:
            # 无论是被取消还是自然结束，都别把画面留在半路上
            if self._emulator.hue_shift:
                self._emulator.hue_shift = 0.0
                try:
                    self.refresh()
                except Exception:  # noqa: BLE001 - 拆卸阶段可能已不可刷新
                    pass

    # ------------------------------------------------------------------
    # 数据泵
    # ------------------------------------------------------------------
    async def _pump(self) -> None:
        proc = self._proc
        if proc is None:  # pragma: no cover - 防御
            return

        loop = asyncio.get_running_loop()
        readable = asyncio.Event()
        # ★ 走 PtyProcess 的抽象，而不是直接 loop.add_reader：
        #   Windows 的 ProactorEventLoop 只支持 socket，管道句柄不行，
        #   那边内部换成"读线程 + call_soon_threadsafe"。
        proc.attach_reader(loop, readable.set)

        dirty = False
        last_frame = 0.0
        eof = False

        try:
            while True:
                if not readable.is_set():
                    try:
                        await asyncio.wait_for(readable.wait(), FRAME_SECONDS)
                    except asyncio.TimeoutError:
                        pass
                readable.clear()

                data = proc.read_all(limit=_READ_LIMIT)
                if data is None:
                    eof = True
                    break
                if data:
                    self._emulator.feed(data)
                    dirty = True
                    # shell 可能刚通过私有 OSC 切换了渐变状态
                    if self._emulator.gradient_rev != self._gradient_rev_seen:
                        self._gradient_rev_seen = self._emulator.gradient_rev
                        self._sync_animation()

                now = time.monotonic()
                elapsed = now - last_frame
                if dirty and elapsed >= FRAME_SECONDS:
                    # 到帧边界了，画一次
                    dirty = False
                    last_frame = now
                    self.refresh()
                elif dirty:
                    # 还没到下一帧：喘口气，把控制权交回事件循环，
                    # 否则持续可读会把这个循环变成忙等，饿死 UI
                    await asyncio.sleep(min(FRAME_SECONDS - elapsed, 0.005))
        except asyncio.CancelledError:
            raise
        finally:
            try:
                proc.detach_reader(loop)
            except (OSError, ValueError):  # pragma: no cover
                pass

            if dirty:
                self.refresh()

        # 只有循环正常结束（读到 EOF）才会走到这里。
        # 放在 finally **外面**，这样这里可以安全 await，
        # 而且任务被取消时不会掩盖 CancelledError。
        if eof:
            self._exit_code = await self._collect_exit_code()
            self.post_message(self.Exited(self, self._exit_code))

    async def _collect_exit_code(self, timeout: float = 0.3) -> int:
        """取回子进程的真实退出码。

        读到 EOF 和进程被回收之间有极小时间窗，所以短暂轮询；
        拿不到就返回 0 —— 绝不阻塞 UI。
        """
        proc = self._proc
        if proc is None:
            return 0

        # Windows：没有 waitpid，退出码从进程句柄上取
        if not HAVE_PTY:
            return proc.exit_code_windows() if hasattr(
                proc, "exit_code_windows") else 0

        if proc.pid is None or proc.pid <= 0:
            return 0

        deadline = time.monotonic() + timeout
        while True:
            try:
                pid, status = os.waitpid(proc.pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                return 0
            if pid == proc.pid:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                if os.WIFSIGNALED(status):
                    return 128 + os.WTERMSIG(status)
                return 0
            if time.monotonic() >= deadline:
                return 0
            await asyncio.sleep(0.01)

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render(self):
        return self._emulator.render_text()

    # ------------------------------------------------------------------
    # 输入
    # ------------------------------------------------------------------
    async def on_key(self, event: events.Key) -> None:
        if event.key == self._release_focus_key:
            # 放开焦点，让 App 级绑定（比如退出）重新生效
            self.app.set_focus(None)
            return

        proc = self._proc
        if proc is None or self._exit_code is not None:
            # 子进程已经结束（或压根没起来）：**绝不能继续吞按键**，
            # 否则 App 级绑定会全部失效，用户被困在 TUI 里出不去。
            return

        payload = _encode_key(event)
        if payload:
            event.stop()
            proc.write(payload)

    # ------------------------------------------------------------------
    # 尺寸
    # ------------------------------------------------------------------
    async def on_resize(self, event: events.Resize) -> None:
        rows, cols = event.size.height, event.size.width
        if rows < 1 or cols < 1:
            return
        self._emulator.resize(rows, cols)
        if self._proc is not None:
            self._proc.resize(rows, cols)
        self.refresh()

    # ------------------------------------------------------------------
    def send(self, data: str | bytes) -> bool:
        """直接往子进程 stdin 写数据（测试/脚本用）。"""
        if self._proc is None:
            return False
        return self._proc.write(data)
