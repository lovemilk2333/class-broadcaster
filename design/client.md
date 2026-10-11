# Windows 客户端设计

客户端运行在 Windows 10 班级大屏上，产品名为 `lovemilk class broadcaster`。程序启动后常驻系统托盘；首次启动或没有已确认的服务端时打开配置窗口，已有有效连接时默认隐藏主窗口。

## 运行入口与托盘

- `client/app.py` 是 PySide6 入口，网络连接在 `QThread` 中运行，UI 线程只处理 signal/slot。
- 托盘菜单只提供“设置”（退出仅在关于页连续点击版本号打开的开发者面板中）。关闭配置窗口只隐藏窗口，不会停止连接。
- 首次启动（无 `data/state.json`）或没有任何已信任服务端时，自动打开配置页；Windows 打包启动器会设置 `QT_PLUGIN_PATH` / `QT_QPA_PLATFORM_PLUGIN_PATH`，确保能加载 `qwindows.dll`。
- Windows 启动器 `lovemilk-class-broadcaster.exe` 与更新器 `lovemilk-class-broadcaster-updater.exe` 使用 `assets/icons/mkcb.ico` 作为 PE 图标（`make windows-icon` / `rsrc` 生成 `rsrc_windows_amd64.syso` 后由 `go build` 链入）。
- 客户端身份由 Ed25519 公钥的 SHA-256（SPKI）指纹表示；界面统一显示为 `SHA256:<base64>`。TLS 证书和原始公钥只在握手内存中使用，不写入信任列表；协议握手仍携带原始公钥用于证书绑定。
- 数据写在安装目录下的 `data/`，适配系统 C 盘重启还原：`identity.enc` 与统一的 `state.json`（含 servers / TTS / 已应用更新 / VC++ 提示 / 日志阈值等）。客户端 TLS 证书每次启动后由私钥临时生成，不落盘。

## 首次启动与配置页

首次启动生成 Ed25519 密钥对和临时自签名客户端证书，配置页显示客户端唯一标识 (公钥 SHA256)，用户可以输入 IP/子网/广播地址扫描服务端、手动指定地址、核对并信任服务端公钥指纹，以及查看连接状态、配置 snapshot ID、最近错误和最后成功连接时间。

服务端扫描使用 UDP `39001`，业务连接使用 TCP/TLS `39002`。扫描响应只携带服务端地址、端口、协议版本、名称、公钥和指纹；客户端验证签名、nonce 和公钥指纹后才显示候选。发现超时为 2 秒，重复响应按服务端指纹去重。服务端指纹变化时必须重新确认，不能因为地址相同而自动替换。所有扫描都有超时和速率限制。

扫描地址支持：
- `255.255.255.255` / `auto`：枚举本机全部 IPv4 网卡，**在每块网卡上 bind 源地址**后发送 limited broadcast 与该网卡的定向广播（如 `10.22.33.255`）。多网卡时不能只从默认路由 NIC 发 `255.255.255.255`，否则到不了其它网段上的服务端；
- CIDR（例如 `10.22.33.1/24` 规范为 `10.22.33.0/24`）：先发该网段定向广播，再逐主机单播；
- 单主机：仅单播。

发现协议是应用层 UDP `DISCOVERY_REQ`/`DISCOVERY_RESP`（不是 ICMP，也不是业务 `PING`）。网段展开最多 65536 个地址，在后台线程执行。配置页服务端列表为左右两列：左侧仅显示**尚未保存**的扫描结果，右侧为“已保存 / 已连接”（扫描到已保存的服务端只出现在右侧）。操作只有「连接」和「删除」：连接 = 保存 + 连接 + 启用 `auto_connect`；删除只删除并断开。可**同时连接**多个 `host:tcp_port`，每个独立 `ConnectionWorker`；启动时仅自动连接 `auto_connect=true` 且有有效指纹的服务端。每个服务端独立保存 `config_id` 与显示默认值，配置状态按当前选中项显示，不会被其它连接覆盖。手动连接默认 TCP `39002`。首次连接先读取 TLS 证书 SPKI SHA-256，用户确认后才写入信任。消息回执按 `source_host`/`source_tcp_port` 路由回对应连接。

管理端日志筛选由服务端内存日志 API 执行，前端仅渲染当前已取回的页。开始/结束日期、最低日志级别和指定级别条件之间使用 AND；指定级别多选项内部使用 OR。例如最低级别为 `WARN` 且指定 `ERROR` 时只显示 ERROR。日期按 `Asia/Taipei` 的自然日边界解释。服务端每次返回 100 条，前端再按共享分页大小（10 至 200）切分显示，切页时按需请求相邻后端页。

配置页面采用与前端类似的 "左侧大类右侧配置内容" 的布局, 参考常见软件的设置页面

## Windows 自启动

- 首次在 Windows 上启动时默认写入当前用户启动项 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MKCBClient`（`state.json` 的 `autostart_default_applied` 防止重复强制写入）。
- 设置页可选「本机（所有用户）」：通过 `reg.exe` 写入 `HKLM\...\Run\MKCBClient`，无权限时自动 `ShellExecute runas` 弹出 UAC；取消自启动会同时清理 HKCU/HKLM。
- 开关状态以注册表实际值为准，不另存偏好；写入失败后恢复实际状态并提示错误。非 Windows 平台禁用相关控件。
- 新消息/撤回提示显示时播放一次 Windows「通知」系统音（`Notification.Default` / `winsound.PlaySound`），与 TTS 是否成功无关。
- 超长单行消息滚动：开头停顿 ≥0.5s，再在剩余显示时间内滚完整段文字，结尾再停 ≥0.5s；窗口仍打开时循环。显示比率为 0（手动关闭）时用舒适默认速度，仍保留首尾停顿。

## 连接状态机

```text
STOPPED -> DISCOVERING -> CANDIDATE -> TRUST_CONFIRM -> CONNECTING -> ONLINE
                                      ^                         |
                                      |                         v
                                  BACKOFF <- unexpected disconnect
```

- `CONNECTING` 建立 TLS 1.3，校验服务端 SPKI SHA-256 指纹，再发送 `req-connect`。
- 握手额外携带 `listener_mode`（`auto`/`pull`/`listen`）和可选 `listener_port`（默认 `39004`）。连接模式由服务端对已授权客户端的策略决定，客户端设置页不能修改模式；客户端只上报本机能力并可调整监听端口。客户端监听模式使用独立 TLS 线程，只处理服务端带公钥和 nonce 签名 payload 的 PING/PONG 探测，不抢占设置窗口或消息窗口。
- `auto` 试用成功后服务端将连接模式固定为监听并结束试用窗口；试用失败才进入默认 31 天重测等待，客户端不会因后续握手重复开始已完成的成功试用。
- 握手携带当前 `config_id` 字符串或 null；收到服务端 snapshot 后原子保存新的 ID。
- 服务端 snapshot 同时包含监听探测策略：默认探测间隔 60 秒、试用时长 12 小时、失败重测等待 31 天、最大丢包率 10%、监听空闲超时 60 秒。监听空闲超时仅用于监听模式（S→C 探测连接）；客户端 `ListenerWorker` 从 snapshot 读取，缺省 60 秒。
- 每个连接使用握手时 snapshot 的心跳参数。运行期间收到配置变更事件时，先验证完整 snapshot，再切换连接参数。未勾选“立即向现有连接发送配置变更”时，服务端不会额外发送 `ConfigUpdate`，而会在下一条消息内带 `as_default=true` 和默认显示位置/显示比率；客户端先把这些值写入本地全局默认，再按新默认解析并显示该消息。
- 正常关闭由服务端发送关闭事件，客户端确认后进入 `SUSPEND_CHECK`，每 5 分钟用 TCP connect 检测；服务端恢复后重新握手。
- 意外断开按 1、1、1、2、4、8、16、32、64、128 秒退避；达到 128 秒仍失败后进入 5 分钟检测模式。进程退出时取消所有计时器。

服务端“全部客户端”消息：创建时已有已批准客户端则固化目标集合；创建时没有任何已批准客户端则使用通配 `*`，后续批准/连接且消息未过期的客户端会加入投递。客户端按自己的公钥 SHA-256 匹配目标集合，离线期间不需要保持网络连接，重新上线后由服务端从有效期内的 pending 记录补投。服务端下发的语音节点先经过深度和展开上限校验，客户端仍会再次限制重复、停顿和节点类型。

## 消息接收、显示与撤回

消息窗口是独立的无边框顶层置顶窗口，不作为设置窗口的子窗口；收到消息时只激活消息窗口，不把设置页面带到前台。窗口在显示后按屏幕可用区域重新计算九宫格位置，并同时更新 QWidget 与 QWindow 的位置。撤回提示使用完整的可伸缩内容区域并垂直、水平居中，显示为「消息已撤回：」+ 净化后的原始 HTML（保留粗体/斜体/下划线/换行/图片）；TTS 朗读去标签纯文本。有 `br`/`img`/`svg` 或行内强调（`b`/`i`/`u`/font-weight|font-style）时静态富文本展示（高度约 ≤ 屏高 45%），保证中文粗斜体可见；仅颜色等非强调 `span` 仍可走单行 marquee。撤回提示亦可 `force_rich` 保留格式。

服务端消息使用 UUID4 `message_id`。客户端仅在进程内按 `(server_id, message_id)` 保存已观察 ID，重复投递不重复显示或朗读，但仍可重复发送必要回执。去重集合不写入磁盘；客户端重启后允许因服务端重发而再次播报。服务端从最近一次成功发送起计算 `ack_timeout_seconds`，默认值为 300 秒，可在管理端设置为 1 秒至 7 天；每个目标的发送时间和回执状态保存在 SQLite。超时且消息尚未过期时服务端只重发未完成目标；收到 `displayed`/`spoken` 后不再重发，重复或乱序的 `received` 不会回退完成状态或延长重传计时。若消息未启用 TTS，以 `displayed/end` 作为完成回执；启用 TTS 时以 `spoken/end` 或 `failed` 作为终态回执。离线客户端上线后接收有效期内消息；默认超过 24 小时的消息由服务端丢弃并记录 warning，重传不超过消息 TTL。

显示层必须展示内容和服务端发送时间，时间按目标时区格式化为 `YYYY-MM-DD HH:mm:ss+08:00`。默认显示比率为 `0.4`，按 **jieba Token** 计单位（每 Token = 1.0），允许范围为 `0..120`；自动显示时长（包含 TTS 最短时长）封顶 `120` 秒，比率为 0 时只显示整行手动关闭按钮；超长消息保持单行，在窗口内按 **prefer 单 Token 停留** 向左滚动（`MarqueeLabel`：开头 padding 停顿 ≥0.5s → 每 Token 滚入边缘 / 停留 / 滚出 → 结尾停顿 ≥0.5s），文字左右各留 `0.25rem`（相对内容字号）边距；消息窗口最大宽度为窗口所在屏幕可用宽度的 **95%**，不得随全文撑破屏幕。启用 TTS 时自动显示时长取 `min(预计 TTS 总时长 + 5s, 120s)`；未启用 TTS 时仍按 Token × 比率计算。优先级数字越小优先程度越高，可中断当前消息；被中断消息按原优先级和服务端时间 FIFO 顺序回队，本地重播完成后再发终态回执（无 TTS 为 `displayed`，有 TTS 为 `spoken`/`failed`）。

正文字体/字号由管理端按客户端设置：握手上报 `available_fonts`，管理端写入 `default_font_family` / `default_font_size`，经 `ConnectResp` 下发（PATCH 字体后会断开会话以触发重连）。空字体 /「(默认)」→ 平台默认（Windows 微软雅黑 / Linux Noto Sans*）。字号为表达式：空 → 内置 **25vh**；`38px`（16–120）；`1.2em`（相对 38px）；`5vw`/`25vh`（相对消息窗所在屏幕可用宽/高，0.0–100）。单条消息可带 `font_size` 覆盖设备默认。客户端只渲染。侧边距与时间戳字号随正文字号同步。

Linux 教室客户端以 **X11** 为准（启动默认 `QT_QPA_PLATFORM=xcb`），无边框消息窗在映射后多次重定位/置顶，保证九宫格位置可用。Qt 绑定：Linux 优先 **PySide2/Qt5**（Python ≥3.10），Windows 打包仍为 **PySide6**（经 `client/qtcompat.py`）。

收到 `message_withdraw` 时停止该消息尚未完成的显示和 TTS；即使消息已经部分或全部显示，也向用户提示“消息已撤回”并朗读提示。撤回提示沿用该消息的显示比率或服务端默认显示比率，按相同 Token 单位、TTS 最短时长和 120 秒上限计算，比例为 0 时保留手动关闭按钮；显示与 TTS 均结束后再关闭（手动关闭除外）；记录本地事件并回报 `withdrawn`，服务端保留原始回执和撤回 warning。

## 离线 TTS

不使用 gTTS 或其他网络服务。管理端发送消息时由“朗读消息内容”选项决定是否发送语音文本节点；未勾选时客户端不探测 TTS，并记录 `TTS skipped`。**Windows 与 Linux** 均用经典 **`piper-tts`** + `zh_CN-huayan-medium` / `en_US-lessac-medium`（jieba 切中/英段后分别用对应 ONNX）。直接调用 Python 包 API，不使用 `piper` CLI，也不再依赖 piper-plus / Tsukuyomi / NLTK。模型缓存于 `client/tts/`（打包 `Lib/tts/`；`make clean` 不删）。`speech_rate` 为语速倍率（默认 `0.75`，1.0=正常），Piper 使用 `length_scale = 1/speech_rate`。失败时回退 Windows SAPI 5。成功引擎写入偏好文件，后续优先使用上次成功引擎，fallback 时从当前消息开头重新朗读。每次探测都会记录引擎是否可用；全部引擎都失败时仍显示文字，记录明确错误并回报 `spoken=failed`。

启用 TTS 的消息在 **入队时**（`_message_received`）即后台预合成 Piper WAV（内存 plan，不落盘；上限约 8 条）。轮到播放时若预取尚未完成，**先不弹窗**，等该 `message_id` 预取结束（成功或失败）后再显示并立刻朗读；预取失败则弹窗后回退现场合成（Piper→SAPI）。等待期间客户端上报 `ClientActivity`（`0x020C`，`state=processing`），管理端连接状态显示 **处理**；本地日志仅在开始与结束各记一条（`TTS processing started … content=…` / `TTS processing finished … elapsed=…s content=…`）。撤回/手动关闭/抢占时上报 `idle` 并清除等待。撤回/手动关闭丢弃缓存；高优先级抢占打断当前播放（含等待中）但保留被打断消息的缓存以便回队后即时播放。Windows piper-tts 子进程合成时父进程必须边跑边读 stdout（否则 WAV 超过管道缓冲会死锁）；播放用同步 `winsound.SND_MEMORY`（CPython 禁止 `SND_MEMORY|SND_ASYNC`），撤回时旁路 `SND_PURGE`。

语音编辑器产生结构化 BSON 节点，不解析任意 XML/脚本。首版节点只有 `text`、`repeat(count, children)` 和 `pause(duration_ms)`；服务端限制重复次数和嵌套深度，客户端编译成安全的纯文本片段。

## 日志与开发者面板

使用 `loguru` 写本地 `data/client.log`（JSONL），并在业务连接建立后通过 `ClientLog` 包将结构化本地日志批量上传；网络暂时不可用时最多在内存排队 1000 条，优先保留较新的日志。上传记录包含时间、级别、logger 名称和日志消息；不含公钥私钥或 TTS 音频。一般业务日志仍避免完整正文，但 TTS **处理**开始/结束日志会附带纯文本摘要与总耗时，便于管理端排查预合成卡住。本地文件达到 `state.json` 的 `log_max_bytes`（默认 **2 MiB**，范围 64 KiB–64 MiB）时，**就地删除最旧的 `floor(行数/2)` 行**并保留后半段，不再轮转到 `client.log.1/.2/.3`（启动时清理历史轮转备份）。服务端单独按客户端 ID 保留最近 500 条，最多保存 1000 个客户端的日志，并由前端客户端日志 Dialog 读取；新日志通过按客户端节流的 SSE 通知该 Dialog 刷新。这些内容与服务端进程日志完全分开。

管理员从前端断开客户端时，服务端发送 `ClientDisconnect`。客户端不持久化管理员断开状态，并依据握手响应中的服务端 `connection_mode` 处理：监听模式停止自动重连，客户端设置页仍可手动连接；从模式每 5 分钟发送一次带 `reconnect_probe` 的握手，由服务端返回 `connect_allowed` 决定是否建立会话。手动连接使用 `manual_connect` 标记，成功后重置退避状态；失败后继续原有探测间隔，不改变监听/从模式策略。启动时仍会连接上次选中的服务端。服务端在已知客户端主动结束会话（`user_exit` / `update` / `admin`）且 TLS hub 已离线时，**不**再对该客户端累计监听探测丢包，避免人为退出/更新重启污染 `probe_loss_percent`。

开发者面板「Exit client」或更新退出前，客户端对每条在线 TLS 会话发送 `ClientSessionEnd`（`reason=user_exit` 或 `update`），服务端据此显示 **人为终止** / **更新重启中**；未上报而掉线的客户端在 30 秒未重连后显示 **意外终止**。刷出 goodbye 后连接线程会 `keep_running=False` 并退出，避免短暂重握手清掉 `session_end` 后又被记成意外终止。

Windows 设置页含「运行环境」：启动后后台探测 piper-tts / onnxruntime / CRT；若原生 ACCESS_VIOLATION 且本机尚未关闭过提示（`state.json` 的 `vc_redist_prompt.dismissed`），则自动弹窗一次并可选打开 VC++ x64 官方下载；之后仅可从该页手动下载。

客户端本地日志使用独立 `ClientLog` 业务包上传到服务端有界缓冲。管理端日志 Dialog 请求后端分页，默认每页 100 条并按日志时间倒序，使用共享分页大小及页码跳转；SSE 更新仅刷新当前客户端日志页。

关于页连续 7 次点击版本号且每次间隔不超过 1 秒后打开开发者面板。面板显示日志、协议版本、连接状态、服务端指纹和 snapshot ID，并提供关闭自启动和退出程序操作；这些操作不改变普通设置页面的持久状态。

客户端握手携带 `client_version`，值来自仓库根目录 `version.json` 的 **`client`** 字段（经 `client/version.py` 读取；与 **`server`** 字段独立）；服务端管理端显示最近一次握手上报的版本。收到服务端 `UPDATE_AVAILABLE` (`0x0207`) 时，客户端根据 metadata 自动判断客户端/更新器/组合包：用 `download_token` 对推送方 TLS 端口新建短连接拉取 `.tar.zst`（或落盘内联小包）→ 校验 files-v1 清单与嵌入 metadata → 将包与 updater 复制到短路径 `data/updates/apply/<sha16>/pkg.tar.zst`（避免 Windows MAX_PATH）后以 `--pid` 启动独立 updater → 客户端释放单实例锁并退出 → updater 覆盖安装（保留 `data/`）→ 删除 `data/updates/` 暂存目录（保留 `data/state.json` 等状态）→ **自动重新启动** `lovemilk-class-broadcaster(.exe)` 以重新握手并上报新版本。只写英文日志，不弹 Windows 托盘通知。`MKCB_INSTALL_DIR` 未设置（开发模式）时只落盘校验不替换、不退出。管理端只上传更新包并由服务端解析 metadata，发布仅即时推送给在线且已批准的目标，离线目标保留为待发送记录。

## 更新包

更新包仅为 zstd level 3 压缩的 `.tar.zst`（内部直接 tar，顶层含 `metadata.json` 与安装文件，无嵌套 zip）。metadata 含 `components`（如 `["updater","client"]`）、兼容字段 `component`（单组件名或 `bundle`）、`version`、`client_version`、`updater_version`、`platform`、`payload_format`、`sha256`；增量包可含 `incremental=true`。相对上一版本绿色 `.zip`、或 `build/cache/client-package-base/<version>/` 中其它版本快照（最多保留 2 个）可只打变更/新增文件（删除路径时回退全量）。
服务端存 `data/updates/<manifest-digest>.tar.zst`（64 位十六进制；新包 BLAKE3，旧包 SHA-256；API/DB 字段名仍为 `sha256`）；`UPDATE_AVAILABLE` 只推 meta + 鉴权 `download_token`（Ed25519，绑定摘要/client_id/expires）+ 可选 `force`，**不**走管理 HTTP `39003`，也**不**写入握手响应，也**不**视为该客户端已完成。客户端对推送方 `host:tcp_port` **新建** TLS 连接，`ConnectReq.update_download=true` 后发送 `UpdateDownloadReq`（`0x0208`），服务端以 `UpdateDownloadResp`（`0x0209`）分块下发包体；短会话不加入 hub。下载进度日志按 `>0%` / `25%` / `50%` / `75%` / `>99%` 里程碑输出 `%=… speed=…MiB/s`。发布时在线目标即时推送；发布时离线或 `applied_seq` 落后的目标客户端，在下次成功建立业务会话后自动补推最高适用 seq（强制与非强制同一规则）。客户端应用成功后把摘要写入 `state.json` 的 `applied_updates`（含 `pending_report`）；重启重连后发送 `UpdateApplied`（`0x020B`），服务端据此推进该客户端的 `applied_seq`。是否下发由服务端按 `applied_seq` 与握手 `client_version` 判定；客户端一旦收到 `UPDATE_AVAILABLE` 即应用（仅本地已应用过的同一 digest 才跳过并补报 `UpdateApplied`）。已上报 `client_version` ≥ 包内版本的客户端服务端不再补推（旧客户端无 `UpdateApplied` 时的回退）。跨重启拒绝同一包。组合包安装顺序：先 updater 再 client。独立 Go updater 写 `data/updater.log`（与 `client.log` 分离；上传进度偏移在 `state.json` 的 `updater_log_offset`）。`Makefile` 同时生成：绿色安装 `bin/*-windows-amd64.zip`（始终全量，首次部署解压即用，管理端发布不接受）与更新包 `bin/*-windows-amd64.tar.zst`（默认可增量）。

客户端发行目录由 CPython 3.11 Windows x64 embeddable runtime、PySide6 Essentials Windows wheels、优化字节码以及 Go GUI 启动器组成。启动器设置安装根目录后运行 `runtime/pythonw.exe -O -m client`；状态始终写入安装根目录的 `data/`。独立 Go updater 写 `data/updater.log`；成功覆盖后以 detached 方式重启同目录启动器，避免更新后进程不在线、管理端版本/在线状态不同步。Linux 使用 `scripts/build_embedded_client.py` 直接组装 Windows 包，不运行 PyInstaller；构建器拒绝混入 Linux `.so` 并检查入口、updater、Python runtime 均为 x86-64 PE。

## 私钥与状态存储

Ed25519 私钥以 `identity.enc` 密文保存。密码由安装时间戳、客户端公钥 SHA-256 指纹、CPUID、设备 ID 和固定域分隔符派生；使用 Argon2id（64 MiB、3 次、2 并行）得到 AES-256-GCM 密钥。salt、nonce 和 KDF 参数写入密文封装，派生密码和 key 只在内存短暂存在。

`state.json` 是客户端唯一本地偏好仓库（`version: 1`），按 key 区分：`servers` / `server`（endpoint + SPKI 指纹）、`config_id`、`listener_mode` / `listener_port`、`autostart_default_applied`、`log_max_bytes`、`tts`（引擎名 + `saved_at`）、`applied_updates`（`digests` + `pending_report`）、`vc_redist_prompt`、`updater_log_offset`。后续新增持久化字段也写入此文件。不保存消息正文、回执或消息去重记录。写入使用临时文件、fsync 和原子替换；损坏时回到空状态并要求重新信任服务端。启动时一次性迁移并删除旧旁路文件（`applied_updates.json`、`tts-engine.json` / `tts.json`、`vc-redist-prompt.json`、`updater.log.offset`）。

## 首版验收场景

- 首次扫描、确认指纹、连接和重启后自动恢复连接。
- 服务端配置 ID 不一致或超过 7 天时重新下发 snapshot。
- 断网、服务端正常关闭、服务端崩溃三种重连路径。
- 同一 UUID4 重传只显示/朗读一次，撤回后显示撤回提示。
- SAPI 不可用时 Piper 离线朗读；两者都不可用时文字仍可见。
- Windows 自启动复选框始终反映注册表实际状态。
