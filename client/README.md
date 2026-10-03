# MKCB 客户端开发环境

客户端固定使用 Python 3.8，并由 uv 管理虚拟环境和依赖。

```bash
cd client
uv python install 3.8
uv venv --python 3.8
uv sync
```

从仓库根目录运行测试：

```bash
uv run --project client python -m unittest discover -s client/tests -q
```

日常运行客户端脚本时使用 `uv run --project client ...`，不要直接调用系统 Python 或 pip。

启动 Windows 客户端托盘与配置窗口：

```bash
uv run --project client python -m client
```

首次运行会在客户端安装目录的 `data` 子目录生成安装绑定加密身份。服务端开机自启动开关直接读写当前 Windows 用户的 Run 注册表项；开关状态不会写入客户端配置文件。
