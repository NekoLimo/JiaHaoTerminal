# vendor/

第三方预编译产物，随项目一起分发。

## fastfetch-linux-x86_64

* 来源：<https://github.com/fastfetch-cli/fastfetch/releases> 2.69.0
  的 `fastfetch-linux-amd64.tar.gz`
* 许可：MIT（见 `fastfetch-LICENSE.txt`）
* 处理：`strip --strip-all`，12 MB -> 2.9 MB
* 用途：`ff` / `fastfetch` 命令直接调用它，拿到**真的**系统信息

按 `<系统>-<架构>` 命名。运行时按 `platform.system()` +
`platform.machine()` 查找；找不到就退回纯 Python 实现
（见 `src/jiahao/utils/fakecmd.py` 的 `_fallback_fastfetch`），
所以缺了这个文件也不会崩，只是信息少一些。

要加别的架构（比如 aarch64），下载对应 release 里的
`fastfetch-linux-aarch64.tar.gz`，strip 之后放到
`vendor/fastfetch-linux-aarch64` 即可，代码不用改。
