#!/usr/bin/env python3
"""检索并下载微信公众号公开文章。

改进点：
- 支持三种方式：历史页接口、搜狗公众号入口、搜狗文章检索（订阅栏/无 history link 场景）。
- 支持手动注入 Cookie，提升在受限网络/风控场景下的可用性。
- 对请求增加重试和退避。
- 支持仅导出文章索引（不下载正文）。
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

SOGOU_ACCOUNT_SEARCH = "https://weixin.sogou.com/weixin"
WECHAT_HISTORY_API = "https://mp.weixin.qq.com/mp/profile_ext"
SOGOU_ARTICLE_SEARCH = "https://weixin.sogou.com/weixin"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HttpClient:
    def __init__(self, cookie: str = "", retries: int = 3, backoff_s: float = 1.0) -> None:
        self.opener = build_opener(HTTPCookieProcessor())
        self.cookie = cookie
        self.retries = max(1, retries)
        self.backoff_s = max(0.0, backoff_s)

    def get(self, url: str, timeout: int = 20, referer: str = "") -> Tuple[str, str]:
        headers = {
            "User-Agent": UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if referer:
            headers["Referer"] = referer

        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = Request(url, headers=headers)
                with self.opener.open(req, timeout=timeout) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    raw = resp.read()
                    encoding = "utf-8"
                    if "charset=" in content_type:
                        encoding = content_type.split("charset=")[-1].split(";")[0].strip() or "utf-8"
                    data = raw.decode(encoding, errors="ignore")
                    return data, resp.geturl()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt == self.retries:
                    break
                time.sleep(self.backoff_s * attempt + random.uniform(0.05, 0.2))
        raise RuntimeError(f"请求失败: {url} -> {last_exc}")


def safe_filename(name: str, limit: int = 120) -> str:
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", "_", name).strip()
    return (name or "untitled")[:limit]


def find_first_account_entry_url(search_html: str) -> str:
    patterns = [
        r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"',
        r'<a[^>]+uigs="account_name_[^"]*"[^>]+href="([^"]+)"',
    ]
    href = ""
    for pattern in patterns:
        m = re.search(pattern, search_html)
        if m:
            href = html.unescape(m.group(1))
            break
    if not href:
        raise RuntimeError("未在搜狗结果页找到公众号入口，请尝试使用 --history-url 方式。")

    if href.startswith("/link?"):
        q = parse_qs(urlparse("https://weixin.sogou.com" + href).query)
        return unquote(q.get("url", [""])[0])
    return href


def find_account_history_url(client: HttpClient, account_name: str, timeout: int = 20) -> str:
    search_url = f"{SOGOU_ACCOUNT_SEARCH}?{urlencode({'type': 1, 'query': account_name, 'ie': 'utf8'})}"
    search_html, _ = client.get(search_url, timeout=timeout, referer="https://weixin.sogou.com/")
    account_url = find_first_account_entry_url(search_html)
    if not account_url:
        raise RuntimeError("无法解析搜狗跳转链接。")

    _, final_url = client.get(account_url, timeout=timeout, referer="https://weixin.sogou.com/")
    if "mp.weixin.qq.com" not in final_url:
        raise RuntimeError(f"未获得微信历史页链接，当前链接为: {final_url}")
    return final_url


def parse_history_params(history_url: str) -> Dict[str, str]:
    q = parse_qs(urlparse(history_url).query)

    def first(k: str, d: str = "") -> str:
        return q.get(k, [d])[0]

    params = {
        "__biz": first("__biz"),
        "uin": first("uin"),
        "key": first("key"),
        "pass_ticket": first("pass_ticket"),
        "wxtoken": first("wxtoken", "777"),
        "scene": first("scene", "124"),
    }
    if not params["__biz"]:
        raise RuntimeError("历史链接中缺少 __biz 参数。")
    return params


def parse_msg_list(payload: Dict) -> List[Dict]:
    raw = payload.get("general_msg_list", "")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []

    out: List[Dict] = []
    for item in parsed.get("list", []):
        ext = item.get("app_msg_ext_info") or {}
        if not ext:
            continue
        dt = int(item.get("comm_msg_info", {}).get("datetime", 0) or 0)
        out.append({"title": ext.get("title", ""), "content_url": ext.get("content_url", ""), "datetime": dt})
        for sub in (ext.get("multi_app_msg_item_list") or []):
            out.append({"title": sub.get("title", ""), "content_url": sub.get("content_url", ""), "datetime": dt})
    return out


def normalize_article_url(url: str) -> str:
    url = html.unescape((url or "").replace("\\/", "/"))
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://mp.weixin.qq.com" + url
    return url


def collect_articles(client: HttpClient, history_params: Dict[str, str], max_pages: int, sleep_s: float, timeout: int) -> List[Dict]:
    all_articles: Dict[str, Dict] = {}
    offset = 0

    for _ in range(max_pages):
        api_url = (
            f"{WECHAT_HISTORY_API}?action=getmsg&__biz={quote_plus(history_params['__biz'])}&f=json"
            f"&offset={offset}&count=10&is_ok=1&scene={history_params['scene']}"
            f"&uin={quote_plus(history_params['uin'])}&key={quote_plus(history_params['key'])}"
            f"&pass_ticket={quote_plus(history_params['pass_ticket'])}&wxtoken={quote_plus(history_params['wxtoken'])}&x5=0"
        )
        body, _ = client.get(api_url, timeout=timeout, referer="https://mp.weixin.qq.com/")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"历史接口返回非 JSON 数据，可能被拦截。原始片段: {body[:160]!r}") from exc

        if payload.get("ret") not in (0, "0", None):
            raise RuntimeError(f"微信历史接口错误: ret={payload.get('ret')} errmsg={payload.get('errmsg')}")

        items = parse_msg_list(payload)
        if not items:
            break

        for it in items:
            u = normalize_article_url(it.get("content_url", ""))
            if u:
                it["content_url"] = u
                all_articles[u] = it

        can_continue = int(payload.get("can_msg_continue", 0))
        next_offset = int(payload.get("next_offset", 0))
        if can_continue != 1 or next_offset == offset:
            break
        offset = next_offset
        time.sleep(max(0.0, sleep_s))

    return sorted(all_articles.values(), key=lambda x: x.get("datetime", 0), reverse=True)


def extract_sogou_article_results(page_html: str) -> List[Dict]:
    """从搜狗微信文章检索页提取文章链接。

    说明：该模式不依赖 mp/profile_ext 的 session，适合 "ret=-3 no session" 场景。
    """
    results: List[Dict] = []

    block_pattern = re.compile(r'<li[^>]+class="[^"]*news-list[^"]*"[^>]*>(.*?)</li>', re.S)
    link_pattern = re.compile(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
    acc_pattern = re.compile(r'uigs="account_name_[^"]*"[^>]*>(.*?)</a>', re.S)

    for block in block_pattern.findall(page_html):
        lm = link_pattern.search(block)
        if not lm:
            continue
        href = html.unescape(lm.group(1))
        title_html = lm.group(2)
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        account = ""
        am = acc_pattern.search(block)
        if am:
            account = re.sub(r"<[^>]+>", "", am.group(1)).strip()

        if href.startswith("/link?"):
            q = parse_qs(urlparse("https://weixin.sogou.com" + href).query)
            href = unquote(q.get("url", [""])[0])

        href = normalize_article_url(href)
        if href and "mp.weixin.qq.com" in href:
            results.append(
                {
                    "title": title,
                    "content_url": href,
                    "datetime": 0,
                    "source": "sogou_article_search",
                    "account": account,
                }
            )
    return results


def collect_articles_by_sogou_search(
    client: HttpClient,
    account_name: str,
    max_pages: int,
    sleep_s: float,
    timeout: int,
) -> List[Dict]:
    all_articles: Dict[str, Dict] = {}
    anti_bot_markers = ["请输入验证码", "访问过于频繁", "异常访问", "验证", "security verification"]

    for page in range(1, max_pages + 1):
        params = {"type": "2", "query": account_name, "ie": "utf8", "s_from": "input", "page": str(page)}
        url = f"{SOGOU_ARTICLE_SEARCH}?{urlencode(params)}"
        html_text, _ = client.get(url, timeout=timeout, referer="https://weixin.sogou.com/")
        lower_html = html_text.lower()
        if any(marker in html_text for marker in anti_bot_markers) or "captcha" in lower_html:
            raise RuntimeError("搜狗页面触发验证码/风控，请稍后重试，或换网络环境后再试。")

        page_items = extract_sogou_article_results(html_text)
        if not page_items:
            break

        new_count = 0
        for item in page_items:
            link = item["content_url"]
            if link not in all_articles:
                all_articles[link] = item
                new_count += 1

        if new_count == 0:
            break
        time.sleep(max(0.0, sleep_s))

    return list(all_articles.values())


def download_articles(client: HttpClient, articles: List[Dict], output_dir: Path, timeout: int, sleep_s: float) -> Tuple[int, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    html_dir = output_dir / "html"
    html_dir.mkdir(parents=True, exist_ok=True)

    ok = 0
    fail = 0
    for i, a in enumerate(articles, start=1):
        title = a.get("title") or f"article_{i}"
        filename = f"{i:04d}_{safe_filename(title)}.html"
        path = html_dir / filename
        try:
            body, _ = client.get(a["content_url"], timeout=timeout, referer="https://mp.weixin.qq.com/")
            path.write_text(body, encoding="utf-8")
            a["local_file"] = str(path.relative_to(output_dir))
            ok += 1
            print(f"[OK] {title}")
        except Exception as exc:  # noqa: BLE001
            a["download_error"] = str(exc)
            fail += 1
            print(f"[FAIL] {title}: {exc}")
        time.sleep(max(0.0, sleep_s))

    (output_dir / "articles.json").write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")
    return ok, fail


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="检索并下载指定微信公众号公开文章到本地")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--account", help="公众号名称（通过搜狗微信检索）")
    src.add_argument("--history-url", help="已知公众号历史页 URL（推荐，稳定性更高）")
    ap.add_argument(
        "--mode",
        choices=["auto", "history", "sogou-articles"],
        default="auto",
        help="抓取模式：auto(默认，失败自动回退) / history(仅历史接口) / sogou-articles(仅搜狗文章检索)",
    )
    ap.add_argument("--output", default="wechat_downloads", help="输出目录")
    ap.add_argument("--max-pages", type=int, default=50, help="最多翻页数")
    ap.add_argument("--sleep", type=float, default=1.0, help="请求间隔秒")
    ap.add_argument("--timeout", type=int, default=20, help="请求超时")
    ap.add_argument("--cookie", default="", help="可选：手动传入 Cookie 字符串")
    ap.add_argument("--cookie-file", default="", help="可选：从文件读取 Cookie 字符串")
    ap.add_argument("--retries", type=int, default=3, help="单次请求失败重试次数")
    ap.add_argument("--list-only", action="store_true", help="仅导出文章索引，不下载 HTML")
    return ap


def resolve_cookie(args: argparse.Namespace) -> str:
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        return Path(args.cookie_file).read_text(encoding="utf-8").strip()
    return ""


def main() -> int:
    args = build_parser().parse_args()
    cookie = resolve_cookie(args)

    client = HttpClient(cookie=cookie, retries=args.retries)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.mode == "sogou-articles":
            if not args.account:
                raise RuntimeError("--mode sogou-articles 需要配合 --account 使用。")
            print(f"[*] 使用搜狗文章检索模式，关键词: {args.account}")
            articles = collect_articles_by_sogou_search(client, args.account, args.max_pages, args.sleep, args.timeout)
        elif args.history_url:
            history_url = args.history_url
            print("[*] 使用手工提供的历史页 URL")
            print(f"[*] 历史页入口: {history_url}")
            params = parse_history_params(history_url)
            articles = collect_articles(client, params, args.max_pages, args.sleep, args.timeout)
        else:
            print(f"[*] 搜索公众号: {args.account}")
            if args.mode == "history":
                history_url = find_account_history_url(client, args.account, timeout=args.timeout)
                print(f"[*] 历史页入口: {history_url}")
                params = parse_history_params(history_url)
                articles = collect_articles(client, params, args.max_pages, args.sleep, args.timeout)
            else:
                # auto: 先试历史接口，失败后自动回退到搜狗文章检索
                try:
                    history_url = find_account_history_url(client, args.account, timeout=args.timeout)
                    print(f"[*] 历史页入口: {history_url}")
                    params = parse_history_params(history_url)
                    articles = collect_articles(client, params, args.max_pages, args.sleep, args.timeout)
                except Exception as history_exc:  # noqa: BLE001
                    print(f"[!] 历史接口模式失败：{history_exc}")
                    print("[*] 自动回退到搜狗文章检索模式（适配订阅栏/无history链接场景）")
                    articles = collect_articles_by_sogou_search(
                        client, args.account, args.max_pages, args.sleep, args.timeout
                    )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] 抓取流程失败: {exc}")
        return 1

    if not articles:
        if args.mode == "sogou-articles":
            print("[!] 未检索到文章。可能原因：关键词无匹配、搜狗未收录、或命中轻度风控。")
            print("[!] 建议：1) 换一个更准确的公众号名称；2) 增大 --max-pages；3) 增加 --sleep 后重试。")
        else:
            print("[!] 未检索到文章，可能被风控或参数过期。建议改用 --history-url 并附带 --cookie。")
        (output_dir / "articles.json").write_text("[]\n", encoding="utf-8")
        return 2

    print(f"[*] 共检索到 {len(articles)} 篇文章")
    if args.list_only:
        (output_dir / "articles.json").write_text(json.dumps(articles, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[*] 已导出索引：{output_dir / 'articles.json'}")
        return 0

    print(f"[*] 开始下载到: {output_dir / 'html'}")
    ok, fail = download_articles(client, articles, output_dir, args.timeout, args.sleep)
    print(f"[*] 完成：成功 {ok}，失败 {fail}。索引文件：{output_dir / 'articles.json'}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
