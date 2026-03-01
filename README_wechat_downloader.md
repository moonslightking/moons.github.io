# 微信公众号公开文章下载工具

脚本：`wechat_public_account_downloader.py`

## 功能
- 支持通过**公众号名**（搜狗微信）或**历史页 URL** 两种方式抓取文章。
- 分页检索公开文章，自动去重并按时间排序。
- 下载每篇文章为本地 `HTML` 文件。
- 输出 `articles.json` 索引（标题、链接、时间戳、下载结果等）。
- 支持重试、请求间隔、手动 `Cookie` 注入。

## 快速开始
### 方式 1：通过公众号名称检索
```bash
python3 wechat_public_account_downloader.py \
  --account "公众号名称" \
  --output wechat_downloads
```

### 方式 2（推荐）：直接使用历史页 URL
```bash
python3 wechat_public_account_downloader.py \
  --history-url "https://mp.weixin.qq.com/mp/profile_ext?..." \
  --cookie-file cookie.txt \
  --output wechat_downloads
```

> 说明：某些环境下搜狗/微信会触发风控；直接用 `--history-url` + `--cookie` 往往更稳定。

## 常用参数
- `--max-pages`：最多翻取的历史页数（每页约 10 条）。
- `--sleep`：请求间隔秒数。
- `--timeout`：HTTP 请求超时秒数。
- `--retries`：单请求失败重试次数。
- `--cookie`：直接传入 Cookie 字符串。
- `--cookie-file`：从文件读取 Cookie 字符串。
- `--list-only`：仅导出 `articles.json`，不下载 HTML。

## 输出结构
```text
wechat_downloads/
  ├─ html/
  │   ├─ 0001_文章标题.html
  │   └─ ...
  └─ articles.json
```

## 注意事项
- 仅用于抓取**公开可访问**内容，请遵守平台规则与版权要求。
- 若出现风控或参数失效：
  1. 增加 `--sleep`；
  2. 使用 `--history-url`；
  3. 提供浏览器中的有效 `Cookie`（`--cookie` / `--cookie-file`）。
