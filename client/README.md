# MKCB 客户端开发环境

客户端固定使用 CPython 3.11，并由 uv 管理开发环境和依赖。

```bash
cd client
uv python install 3.11
uv venv --python 3.11
uv sync
```

从仓库根目录运行测试：

```bash
uv run --project client python -m unittest discover -s client/tests -q
```

日常运行客户端脚本时使用 `uv run --project client ...`，不要直接调用系统 Python 或 pip。

Linux 可直接组装 Windows x64 多文件包，无需 PyInstaller 或 Wine：

```bash
make client
# 或显式：make build-client
```

构建器下载官方 CPython 3.11 embeddable runtime，通过 `uv` 安装 Windows x64 wheels，生成 `-O` 优化字节码，并交叉编译 Go GUI 启动器。中间产物在 `build/client/`，可发版包在 `bin/`（绿色 zip + 更新 tar.zst）。若 `build/` 已存在、只需再打包：`make package-client`。

启动 Windows 客户端托盘与配置窗口：

```bash
uv run --project client python -m client
```

首次运行会在客户端安装目录的 `data` 子目录生成安装绑定加密身份。服务端开机自启动开关直接读写当前 Windows 用户的 Run 注册表项；开关状态不会写入客户端配置文件。
