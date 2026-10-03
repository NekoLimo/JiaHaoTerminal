# 超级牛逼嘉豪终端

一个 TUI：左边是内嵌终端（跑自研的 `jiahaoshell`），右边上半是旋转的 ASCII 地球、
下半是代码雨。附带一批"业务模块"命令。

```
┌──────────────────────────────────────┐ ┌────────────── WORLD ──────────────┐
│                SHELL                 │ │"""ASCII**aem1k.com/world**MartinK│
│  /home/limo/jiahao/JiaHao # scan     │ │leppeJS""##`          `1536**20**y│
│  [*] 目标: /usr                       │ │=046514->      **        331162->4│
│  [*] 初始化 DISK-REAVER v4.2.1       │ │+3+3+1+1   ##########     05d**x20│
│                                      │ └──────────────────────────────────┘
│                                      │ ┌─────────────── SRC ───────────────┐
│                                      │ │t " W % D   k z k }   c   T . j   │
│                                      │ │a ` q L `   J m Y H   z   F T     │
└──────────────────────────────────────┘ └──────────────────────────────────┘
```

## ⚠️ AI 生成声明

**本项目的代码由 AI 编程助手生成**（模型 `deepseek-flash`），人类负责提需求、
做技术决策、**在真机上验证并发现问题**。

这不是走过场 —— 项目里几个最隐蔽的 bug 全都**只有真机运行才暴露**：
ConPTY 的 `lpValue` 传错（API 全返回成功但读不到数据）、
`STARTF_USESHANDLES` 让子进程丢掉 stdin、伪控制台不自动关闭导致退不出 TUI。

**AI 自己写的测试当时全是绿的。**

完整说明（含各平台验证状态、版权归属提醒）见 [AI-DISCLOSURE.md](AI-DISCLOSURE.md)。


## 平台支持

| 功能 | Linux | macOS | Windows |
|---|---|---|---|
| 内嵌终端（pty / ConPTY） | ✅ | ✅ | ⚠️ 已实现，未实机验证 |
| 作业控制（`jobs`/`fg`/`bg`/`kill %N`） | ✅ | ✅ | ❌ 需要 fork |
| 外部命令（`ls`/`cat`/…） | ✅ | ✅ | ⚠️ 走 `subprocess`，无作业控制 |
| 系统信息（`fastfetch`） | ✅ `/proc` | ✅ `sysctl` | ✅ `ctypes` |
| 代码雨 / 地球 / 业务模块 | ✅ | ✅ | ✅ |

**Linux 和 macOS 完整可用。**

**Windows**：内嵌终端走 ConPTY（`widgets/conpty.py`），结构布局和调用序列
都有测试覆盖，但**作者没有 Windows 机器，未经实机验证** —— 第一次跑
可能会遇到需要调的地方。作业控制仍然不支持（那是 fork/进程组语义）。

### ConPTY 是怎么接上的

POSIX 用 `pty.fork()` + master fd，Windows 是另一套 API，差异全部收在
`PtyProcess` 内部：

| | POSIX | Windows |
|---|---|---|
| 创建 | `pty.fork()` | `CreatePseudoConsole` |
| 接进程 | `execvp` 自动继承 | `CreateProcessW` + `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` |
| 读写 | master fd | 两根管道句柄 |
| 就绪通知 | `loop.add_reader(fd)` | **读线程 + `call_soon_threadsafe`** |
| 改尺寸 | `TIOCSWINSZ` | `ResizePseudoConsole` |
| 结束 | `SIGTERM`/`SIGKILL` | `ClosePseudoConsole` + `TerminateProcess` |

其中**就绪通知**是关键：`ProactorEventLoop.add_reader` 只支持 socket，
管道句柄不行，所以 Windows 上只能起一个线程阻塞在 `ReadFile` 上。
这就是 `PtyProcess.attach_reader()` 这层抽象存在的原因。

## 跑起来

```bash
uv run jiahao                 # 启动 TUI
uv run jiahao --speed 2       # 慢镜头（>1 更慢）
uv run jiahao --no-con        # 关掉所有动画（测试用）
```

直接跑内层 shell（不需要 pty，Windows 也能用）：

```bash
uv run src/jiahao/jiahaoshell.py
```

## 常用命令

```
scan /usr        扫盘（真读文件系统 + 真算熵值）
低调             向 127.0.0.1 发起 DDoS（打自己）
fastfetch        系统信息（内嵌真 fastfetch）
help             看全部
```

## 跨平台怎么做的

所有平台差异集中在 **`src/jiahao/utils/compat.py`**，别处不再写 `if windows`：

* **系统信息** —— 内存 / 运行时间 / CPU / 磁盘 / 负载，各平台走各自的路
* **能力探测** —— `have_pty()` / `supports_suid()` / `HAVE_FORK`
* **可执行名** —— `exe_name("jiahaoshell")` → Windows 补 `.exe`

两条硬规矩（`compat_test.py` 用 AST 扫描强制）：

1. **拿不到信息就返回占位符，绝不抛异常。** 系统信息用来装点门面，
   为它崩掉整个 shell 不值得。
2. **POSIX 专用调用必须有守卫。** `os.fork` / `os.killpg` / `termios` /
   `signal.SIGKILL` 这些要么判断能力后再用，要么走 `getattr` 兜底。

## 测试

```bash
python3 run_tests.py            # 全套，~17 秒（并行）
python3 run_tests.py --fast     # 核心子集，~9 秒
python3 run_tests.py --docker   # 额外跑容器里的真 pty / Wine ConPTY 套件
python3 run_tests.py --list     # 看有哪些套件
```

### 本机测不了的那部分

开发机的沙箱**开不了 pty**（`/dev/ptmx` 权限特殊），也**不是 Windows**。
所以有两块东西本机验证不了，用 `docker/` 下的环境补上（见 `docker/README.md`）：

| 环境 | 覆盖 | 结果 |
|---|---|---|
| `Dockerfile.pty` | 真 pty 端到端（含 `stty size` 验证 `TIOCSWINSZ`） | ✅ 24/24 |
| `Dockerfile.wine` | ConPTY 实机（结构布局 / 创建 / 句柄 / 失败路径） | ✅ 20 passed, 5 skipped |

Wine 那 5 项 SKIP 是 Wine 自己的限制（ConPTY 数据通道未实现，
[Wine Bug 58967](https://bugs.winehq.org/show_bug.cgi?id=58967)，
至今 ASSIGNED），**在真 Windows 上会变成真实断言** ——
那份代码目前仍然只有静态验证。

`dashboard_test.py` 里的 **node 逐字对比**值得单独提一句：地球的算法移植自
[aem1k.com/world](https://aem1k.com/world)，如果机器上有 `node`，测试会跑一遍
**原版 JS** 并和 Python 输出逐字比对 —— 移植这种代码，肉眼看着像不算数。
没装 node 会优雅跳过。

## 第三方

`vendor/fastfetch-linux-x86_64` —— 预编译的 [fastfetch](https://github.com/fastfetch-cli/fastfetch)
（MIT，见 `vendor/fastfetch-LICENSE.txt`），`ff` 命令直接调它。
按 `<系统>-<架构>` 命名，加别的架构不用改代码。
