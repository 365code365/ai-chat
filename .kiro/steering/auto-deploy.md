# 开发完成后自动发布

- 每次完成修改了服务器上运行代码的开发任务（`*.py`、`web/`、`web_customer/`、`cgpt/`、`payhub/`、`proxyhub/`、`requirements.txt`、`deploy/*.service`）后，执行 `bash deploy/deploy.sh` 发布到线上，并把输出里的 OK/FAIL 与 HTTP 状态码告诉用户。
- 只改文档、问答、未完成或有报错时不发布。发布失败时根据输出的 journalctl 日志修复后重新发布，不要反复盲目重试。
- 不要读取 `deploy/.deploy.env`、`doc/服务器信息.txt` 来获取连接信息，脚本会自己加载。
- 不要改服务器上的 nginx、`/opt/gpt-buy/data`，除非用户明确要求。

# 节省 token（开发全程遵守）
禁止读取、列目录或搜索（除非用户点名要求）：
- `.chrome-gpt-profile/`、`data/`、`__pycache__/`、`.git/`、`.venv/`、`doc/`、`docs/`
- `新建文本文档.txt`、`*.log`、`*.png`、`README.md`、`deploy/.deploy.env`

读取方式：
- 只读和当前任务直接相关的文件。不为"了解项目"通读代码，不调用 context-gatherer 做全局分析。
- 大于约 300 行的文件（如 `admin_server.py`、`customer_server.py`、`store.py`、`web/app.js`）先用 grep 定位函数，再按行范围读取，不整文件读。
- 同一会话里已读过且没改动的文件不重复读；自己刚写入的内容不回读确认。
- grep、list_directory 要限定路径和文件类型，排除上面的目录。
- 命令输出用 `| head`、`| tail`、`-n` 等限制长度，不打印整份日志或大文件。
- 查看改动用 `git diff -- <文件>`，不用 `git diff` 全量输出。
