# 脚本接口

以技能根目录为基准，命令中的地址均为用户提供的企业地址。stdout 是 JSON：成功 `{ok:true,data:{...}}`，失败 `{ok:false,error:{code,message}}` 并退出 1。不要把失败解释成空列表。

| 脚本与命令 | 用途 |
| --- | --- |
| `drive_api.py setup --address URL` | 仅配置部署地址 |
| `drive_api.py status --address URL` | 无网络检查凭据存在性 |
| `drive_api.py login --address URL` | 本人交互终端隐藏输入个人 token |
| `drive_api.py bind-cli --address URL` | 显式复用已有同域 CLI Cookie，核对身份后保存绑定，不复制凭据 |
| `drive_api.py doctor --address URL` | 访问最近使用列表验证只读 API |
| `drive_api.py discover --address URL --query 关键词 [--query 另一个词]` | 首选入口；名称搜索遇5xx则降级到有范围限制的列表名称匹配 |
| `drive_api.py search --address URL --query 关键词 [--query 另一个词]` | 名称查询、去重；每词默认 20 条，最多返回 60 条 |
| `drive_api.py list --address URL --type used` | 最近使用资料；也可 `shared` |
| `drive_api.py list --address URL --type desktop` | 桌面列表，服务端可能忽略limit，客户端仍截断 |
| `drive_api.py list --address URL --folder GUID` | 已知目录的子项 |
| `drive_api.py info --address URL --guid GUID` | 元信息/类型/权限 |
| `drive_api.py read --address URL --guid GUID` | 只读纯文本，不作全文写回 |
| `document.py doctor --address URL` | CLI 版本、当前用户 ID |
| `document.py read --address URL --url DOCUMENT_URL` | 最新 Markdown、head、sha256 |
| `document.py edit --address URL --url DOCUMENT_URL --patch-file FILE --expected-sha HASH` | 精确替换并整份回读核对 |

原始 `search` 参数还有 `--ancestor GUID`、`--space GUID`、`--size 1..100`、`--max-results 1..200`、`--offset 0..10000`。`discover` 支持 `--query`、`--size`、`--max-results`，以及最多两个 `--folder GUID`；指定目录时不会扩大到桌面/共享范围。

名称搜索传 `searchFields=name,file_name`、空字符串 `params`、显式 `ancestorGuids`。服务端 `next` 原样放在 `pages` 中；不自动翻页、不把游标当 offset。`discover` 返回 `method=bounded_name_scan` 时说明发生了列表降级，需阅读 scopes、scanned_count、incomplete 和 primary_search_error。没有命中只表示当前范围未命中。

列表最多输出 100 条，服务端超量时客户端截断。`nextURL` 可能是含 lastTimestamp 的对象，并不总是 URL；脚本不自动跟随。`has_more:null` 保持未知，`page_full` 只表示当前页满。

补丁例子（从刚读取的实际文本生成，不照抄目标内容）：

```json
[{"old":"当前状态：待验证。","new":"当前状态：验证完成。"}]
```

编辑是普通文本变更入口，不提供盲目 append、替换整份文档、删文件、分享、改权限或任意命令执行接口。解析到不支持的结构就停止该写入。

官方依据：[搜索](https://support.shimo.net/apidoc/docs-site/6000001/3470314e0.md)、[列表](https://support.shimo.net/apidoc/docs-site/6000001/3470044e0.md)、[元信息](https://support.shimo.net/apidoc/docs-site/6000001/3470045e0.md)、[纯文本](https://support.shimo.net/apidoc/docs-site/6000001/3470048e0.md)。这是私有部署 `/api/v1`，不是 SaaS `lizard-api`。
