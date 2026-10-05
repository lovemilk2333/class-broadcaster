# lovemilk class broadcaster

> [!WARN]
> This software has made by AI. I only give the basic design for the general structure.

## 版本号

仓库根目录 `version.json` 分开管理服务端与客户端版本：

```json
{
  "server": "0.1.0",
  "client": "0.1.0",
  "build_date": ""
}
```

- `make server` / `make build-server` 使用 **`server`** 版本并产出 `bin/` 发版包
- `make client` / `make build-client` 使用 **`client`** 版本（绿色 zip + 更新 `.tar.zst`）
- 构建时写入各组件 `version.json` 的 `build_date` 为本地时间戳 `yyyy-mm-dd HH:MM:SS.xxxx±xx:xx`

### Make 目标

| 目标 | 作用 |
|------|------|
| `make server` / `make build-server` | 编译 Linux 服务端并打包到 `bin/`（可发版 zip） |
| `make client` / `make build-client` | 编译 Windows 客户端并打包到 `bin/`（zip + tar.zst） |
| `make package-server` / `package-client` | 仅从已有 `build/` 再打 `bin/` 包 |
| `make` / `make package` | `server` + `client` 全量发版 |

## Linux 服务端自启动

### 发布 zip（推荐）

```bash
make server
# 或显式：make build-server
```

产物：

- `bin/lovemilk-class-broadcaster-server-<version>-linux-amd64.zip`
- `bin/lovemilk-class-broadcaster-server-<version>-linux-arm64.zip`

每个 zip 内含服务端 ELF、`lovemilk-class-broadcaster-server.service` 和 `install-user-service.sh`。目标机上：

```bash
unzip lovemilk-class-broadcaster-server-*-linux-amd64.zip
cd lovemilk-class-broadcaster-server-*-linux-amd64
./install-user-service.sh
```

无需 root，也无需源码树。二进制安装到 `~/.local/share/lovemilk-class-broadcaster/bin`，数据在 `data/`。日志：`journalctl --user -u lovemilk-class-broadcaster-server -f`。

### 从源码安装

```bash
make install-server-user-service
```

卸载（保留数据）：`make uninstall-server-user-service`。无登录会话常驻可先：`loginctl enable-linger "$USER"`。

仅重新打包已编译的服务端（不重新 go build）：`make package-server`。
