# 配置与试点验收

## 管理员准备

确认部署版本支持以下现有接口：搜索文件、列目录、文件信息、获取纯文本。为试点用户提供适配服务器和操作系统的 shimocli 安装包；技能 ZIP 不包含 CLI 二进制。客户域名必须从用户获取，不能沿用别的企业域名。

优先复用本人已完成的 CLI 登录。技能通过同域 Cookie 请求现有 API，核对 API 与 CLI 是同一个用户；不需要为这条路径新建管理员 token。该方式已在内部部署验证，其他部署必须实测 Cookie 是否可用。现有 CLI 的凭据格式属于版本兼容依赖，升级后需复核。

仅当部署不接受 Cookie 或企业要求使用 OAuth 时，按[对接流程](https://support.shimo.net/apidoc/docs-site/6000001/338112m0.md)及[获取用户令牌](https://support.shimo.net/apidoc/docs-site/6000001/3469974e0.md)准备本人 access_token。token 获取涉及 client_id、client_secret、grant_type=super_admin、scope、user_id，属于管理员操作；不把 client_secret 或管理员身份下发给普通使用者。通过企业安全渠道交付个人 token、绑定用户 ID、有效期和撤销方式；与 CLI user_id 对照。

## 运行环境检测与补齐

Agent 先检查、缺少再安装、安装后重新检查，最后继续原任务。下列路径以技能目录为基准，实际执行时加引号并使用绝对路径。`check` 只检测依赖；`ensure` 才会安装。两者都不登录、不访问企业资料，也不读取 Cookie。

```sh
# macOS / Linux，首次运行不需要已有 Python
bash scripts/bootstrap.sh check
bash scripts/bootstrap.sh ensure
# 已确认管理员提供了适配该部署的本机 CLI 包时
bash scripts/bootstrap.sh ensure --cli-package '/absolute/path/shimocli-install.sh'
# 原始二进制、tar.gz 或 zip 需同时提供管理员给出的 SHA-256
bash scripts/bootstrap.sh ensure --cli-package '/absolute/path/shimocli.tar.gz' --sha256 '<64位SHA-256>'
```

Windows 使用 PowerShell：

```powershell
& 'C:\absolute\skill\scripts\bootstrap.ps1' check
& 'C:\absolute\skill\scripts\bootstrap.ps1' ensure
& 'C:\absolute\skill\scripts\bootstrap.ps1' ensure --cli-package 'C:\absolute\shimocli.zip' --sha256 '<64位SHA-256>'
```

### Python

- 已有 Python 3.9+ 就直接复用，不为此升级。非标准目录可设置进程环境 `SHIMO_DRIVE_PYTHON` 为解释器绝对路径；无效覆盖值会报错，不静默换一个解释器。
- macOS 缺 Python 时，`ensure` 使用**已有** Homebrew 执行 `brew install python@3.13`，之后按绝对路径查找，避免重开 Agent 才能识别。没有 Homebrew 时，Agent 从 [Python 官方 macOS 下载页](https://www.python.org/downloads/macos/)选择适配安装器并协助安装；不为装 Python 连带安装整套 Homebrew。系统弹窗/管理员权限由用户在操作系统完成。
- Windows 缺 Python 时，使用已有 WinGet 的 `winget install --id Python.Python.3.13 --exact --source winget --scope user --disable-interactivity`。不自动接受额外协议。没有 WinGet、企业策略禁止或需要交互确认时，使用 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)的适配安装器，完成系统交互后再次检测。不要修改 PowerShell 执行策略来绕过限制；策略不允许脚本时，Agent 直接在已允许的终端执行等价检查/官方安装步骤。
- Linux 仅在当前进程本来已有 root 权限时调用已有 apt-get/dnf/apk 安装 python3，不自行 sudo。普通用户用企业批准的 Python 环境，或由管理员通过系统包管理器安装。装到的 Python 仍必须通过 3.9+ 检查。
- 官方依据：[Homebrew Python 3.13](https://formulae.brew.sh/formula/python@3.13)、[WinGet install 参数](https://learn.microsoft.com/en-us/windows/package-manager/winget/install)、[Python 的 WinGet 包清单](https://github.com/microsoft/winget-pkgs/tree/master/manifests/p/Python/Python/3/13)。本版选择 3.13 安装系列，不宣称它是最新 Python。

### 轻文档 CLI

技能 ZIP **不包含 Python 或 shimocli 二进制**，也没有公共 CLI 下载地址。Agent 只能使用用户/管理员已经明确提供、适配该企业服务端的本机安装包；不要自行遍历 Downloads、猜下载站点或用别的企业包。来源不明确就询问安装包来源，不能仅凭文件名认定可信。

`ensure --cli-package` 支持 arm64/x86_64 原生二进制、tar.gz/zip 和已适配的 macOS 自解压 shell 包；Universal 多架构包、需要附带运行库的包和其他安装器交给管理员处理。原始包需要管理员给出的外层 SHA-256；自解压包验证内嵌归档 SHA-256，只取其中 CLI，不运行 shell 包、不触发其中的登录或 Codex Skill 安装。内嵌哈希证明完整性，不证明发布者身份。未知安装器格式/错误平台/架构/哈希不符时停止，交给管理员确认；不跳过校验、不删除隔离属性或关闭系统安全保护。带 macOS 隔离标记的下载包会保留该标记；若系统阻止 CLI 版本验证，安装不会报告成功，需管理员提供可正常执行的可信包或完成正规的系统信任流程。

安装落在当前用户专用目录：macOS/Linux `~/.local/share/shimo-drive/bin/`；Windows `%LOCALAPPDATA%\shimo-drive\bin\`。两个业务适配器共用该目录的发现逻辑，因此不依赖编辑系统 PATH 或重启 WorkBuddy。也可通过 `SHIMO_DRIVE_CLI` 显式指定可信 CLI 的绝对路径；设置无效时停止，不静默回退。

可执行的现有 CLI 会保留，不自动覆盖/升级；版本能运行仍不代表适配所有服务端，随后必须运行身份和业务 `doctor`。缺包时输出 `source_required`，告知管理员系统与架构即可；CLI 格式不兼容时请求相应版本。认证缺失必须走登录流程，重新安装软件不会解决它。

### Agent 的结果处理

`ready:true` 仅表示本机依赖可用。Python 缺少、来源缺失、安装失败、CLI 无法运行分别报告实际阻塞点，不报“环境已完成”。安装成功后执行只读 `doctor` 并接回用户原任务。包管理器下载失败可查网络/代理后重试一次；不要无休止重装，也不要关闭证书校验。仅需 Bearer Drive 只读调用时，Python 就绪后可继续可用的只读功能，同时明确 CLI 编辑尚未就绪。

当前实机环境为 macOS arm64、shimocli 0.2.0。Windows/Linux 提供入口和解析逻辑，仍需客户对应平台包及实机验收，不能说已完成跨平台验证。

## 本机配置

运行环境就绪后，通过启动器使用已检测的 Python（Windows 将 `bash scripts/bootstrap.sh` 换成 PowerShell 的 `& '...\bootstrap.ps1'`）：

```sh
bash scripts/bootstrap.sh run drive_api.py setup --address 'https://your-company.example/desktop'
bash scripts/bootstrap.sh run drive_api.py bind-cli --address 'https://your-company.example/desktop'
bash scripts/bootstrap.sh run drive_api.py doctor --address 'https://your-company.example/desktop'
bash scripts/bootstrap.sh run document.py doctor --address 'https://your-company.example/desktop'
```

`bind-cli` 是显式启用复用的步骤，可由已获授权的 Agent 执行。脚本只从 CLI 管理的固定认证源（`$CODEX_HOME/shimocli/auth.json`，默认 `~/.codex/shimocli/auth.json`）选择该主机名的 Cookie，校验文件权限/非符号链接、CLI/API/绑定用户一致；不复制 Cookie，不读浏览器数据库，不输出凭据。使用 WorkBuddy/豆包工作时也仍使用 CLI 自己管理的同一个来源；不是读取 Agent 的账号。

`setup` 不登录，`status` 不联网，`doctor` 仅验证身份与列表。CLI 未登录时使用管理员提供版本的官方认证入口，由本人在可信交互终端隐藏输入，不把凭据发到聊天。Bearer 备选用 `drive_api.py login --address ...` 隐藏输入；两种认证不会静默切换，有冲突时停止配置。

Drive 配置按 origin 哈希分文件保存在本机：macOS `~/Library/Application Support/shimo-drive/`，Linux `$XDG_CONFIG_HOME/shimo-drive/`（或 `~/.config/shimo-drive/`），Windows `%APPDATA%/shimo-drive/`。Cookie 模式只存 origin、auth_mode 和已验证的用户 ID；Bearer 模式保存个人 token。Unix 目录 700、文件 600；文件不是加密保险库。不要复制该目录到技能包。换账号会触发 identity_mismatch，需明确重新配置，不自动使用另一个人的身份。

自动化可由管理员通过进程环境安全注入 `SHIMO_DRIVE_TOKEN`，同时设置 `SHIMO_DRIVE_ORIGIN` 为该 token 所属 HTTPS origin。两者必须匹配，避免跨客户误发。不在示例中填写真实值。

名称搜索接口若返回 5xx，`discover` 会报告原因并在每类最多 30 个文件的桌面、最近使用、共享列表中匹配名称，或仅扫描用户指定的最多两个目录。结果明确标为不完整；这是现有列表能力的降级，不需要新建知识库或改服务端。列表返回超出 limit 时，客户端也会截断并标注，不能把截断当作全盘没有。

WorkBuddy 导入 ZIP 后在新任务调用“石墨私有化 Drive”。本包脚本需要本地命令执行权限；公司地址、VPN、证书应在电脑上正常可达。出现 TLS 错误应修复网络/证书，不关闭证书校验。Windows/Linux 的 Python 脚本是跨平台设计，仍需相应 CLI 包和实机验收。

## 首次验收

1. 查询一个仅测试用户可见的资料，确认搜索结果和 CLI 身份一致。
2. 用记不清文件名的描述，通过两个近义词找到已知测试资料，不给文档链接。
3. 阅读、修改一个普通句子、精确回读；另一个段落保持不变。
4. 多个候选不误写；未知文件类型不写；身份不一致不写。
5. 只读用户拒写、无权限文件不可见、过期认证、并发修改、未知写入结果不重放。

全部完成后才将该“部署版本 + CLI 版本 + Agent 客户端 + 操作系统”标为可用。本包有自动化单元测试，但它们不替代以上实测。
