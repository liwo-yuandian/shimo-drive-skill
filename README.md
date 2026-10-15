# 石墨私有化 Drive · Agent Skill

让 Codex 根据你的描述查找石墨资料，阅读确认后修改轻文档普通正文，并回读核对结果。你不必先找到文档链接。

适用于**石墨私有化部署**，通过现有 Drive API 和管理员提供的轻文档 CLI 工作。企业地址由每位用户配置，不绑定某个内部环境，不适用于石墨 SaaS。

## 一句话安装到 Codex

在能够执行本地命令的 Codex 桌面版或 CLI 中，复制发送：

```text
请使用 $skill-installer 安装 https://github.com/liwo-yuandian/shimo-drive-skill/tree/main/skills/shimo-drive ，然后检查运行环境，并引导我连接自己的石墨私有化部署。
```

Codex 会读取并安装整个 Skill 目录。新技能在安装后的下一轮对话可用；如果没有出现，重新打开会话或重启客户端。企业管理策略可能限制自定义技能和本地命令，应遵循所在工作区的配置。

安装完成后，可以直接说：

```text
用石墨私有化 Drive 帮我找一下上个月给新同事准备的入职材料，先告诉我找到哪些候选。
```

目标明确后，再说要改什么，例如“把这份材料中的‘培训日期待定’改为‘培训日期为下周三’，其他内容保持不变”。

如需固定版本，可将安装链接中的 `main` 换成 `v0.1.4`。已安装的 Skill 不会因为仓库更新而自动升级；更新时让 Codex 检查版本、备份旧目录，再替换该 Skill，保留单独存储的企业配置。

## 第一次使用需要什么

1. **企业地址**：自己的 HTTPS 根地址或 `/desktop` 地址，例如 `https://your-company.example/desktop`；电脑需能访问企业网络。
2. **Python 3.9+**：Skill 先检测；缺少时优先通过现有 Homebrew、WinGet 或有权限的系统包管理器协助安装。
3. **适配企业部署的 shimocli**：已有可用版本就直接复用。缺少时使用企业管理员提供的本机安装包，并检查完整性、平台和架构。仓库不包含 CLI 二进制，也没有统一公共下载源。
4. **本人登录**：优先显式绑定同域 CLI 登录，由脚本核对 CLI 与 Drive 是同一用户。登录/系统授权需本人完成，不能把 Cookie 或 token 发到聊天、GitHub Issue 或仓库。

安装 Skill、安装依赖和登录企业是三个步骤。Agent 可以协助完成；缺少企业安装包或本人授权时，会明确说明需要补充什么。

详细说明见 [配置与环境补齐](skills/shimo-drive/references/setup.md)。

## 当前能做什么

| 能力 | 当前范围 |
| --- | --- |
| 根据描述找资料 | 提取主题、简称和近义词，按名称与目录查找候选；不是全文检索 |
| 搜索接口异常 | 名称搜索返回 5xx 时，有限扫描桌面/最近使用/共享列表，明确标注结果不完整 |
| 阅读 | 轻文档使用 CLI 阅读；其他支持类型可读取 API 提供的纯文本 |
| 修改 | 普通正文精确替换；核对最新内容，提交后回读，遇到冲突或未知结果不盲目重试 |
| 环境补齐 | 检查 Python、CLI、系统和架构，按现有安装来源协助安装并复检 |

本版不处理表格、图片、附件的结构修改、权限管理或批量修改。每个客户部署仍需验证接口、身份和读写权限。macOS arm64 已实机验证；Windows/Linux 提供入口和适配逻辑，尚需对应平台安装包与实机验收。

资料正文会进入你使用的 Agent 上下文，应符合企业已有的 Agent 使用要求。仓库只分发技能代码，不托管企业资料或登录凭据。

## 为什么主体是 Skill

当前能力已由现有 API 和本机 CLI 完成，Skill 负责组织查找、认证检查、修改和回读流程，安装不需要部署新的中间服务。

仓库同时包含标准 `plugin.json`，复用 `skills/` 中同一份内容，为以后插件分发或增加 Connector/MCP 连接留出入口。**当前验证和推荐的是上面的 GitHub Skill 安装路径**；插件描述文件不代表已上架 Codex 插件目录，也不新增服务端能力。

官方参考：[Codex Skill 创建与安装](https://learn.chatgpt.com/docs/build-skills) · [插件打包](https://developers.openai.com/plugins/build/plugins)。

## 其他 Agent

允许本地脚本执行、支持 `SKILL.md` 的 Agent 可以复用 `skills/shimo-drive/`。WorkBuddy 的 ZIP 使用其所需的额外展示字段，可从 [Releases](https://github.com/liwo-yuandian/shimo-drive-skill/releases) 下载。各平台的企业策略、登录流程和兼容性需要分别验证。

## 维护与验证

```text
skills/shimo-drive/   可直接安装的 Skill，包含工作流、脚本、配置说明
tests/               使用模拟接口和隔离目录的测试，不访问企业数据
plugin.json          标准插件描述，复用 skills/ 中的内容
build_package.py     生成通用 ZIP 和 WorkBuddy ZIP
```

```sh
python3 -m unittest discover -s tests -v
bash -n skills/shimo-drive/scripts/bootstrap.sh
python3 build_package.py
```

运行时仅使用 Python 标准库。测试覆盖现有业务保护和环境检测、安装失败、校验失败、路径解析等行为；自动化通过不替代客户部署验收。

反馈问题时请说明版本、操作系统、错误代码和已脱敏的复现步骤，不上传登录信息或业务文档。
