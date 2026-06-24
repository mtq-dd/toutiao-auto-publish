# 今日头条发布助手

CDP 驱动的今日头条图文文章全自动发布系统，支持四项内容审查和 MCP Server 集成。

**AI Agent 接手请阅读 `docs/AGENT_HANDOFF.md`。**

## 快速开始

```bash
pip install -r tools/requirements.txt
msedge --remote-debugging-port=9222
# 浏览器登录 https://mp.toutiao.com
python tools/auto_publish_final.py --title "标题" --content article.html --images images/
```

详细文档见 [`docs/使用说明.md`](docs/使用说明.md)
