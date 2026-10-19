"""联网搜索（用户 2026-09-29 第十三轮要求：「让他可以上网搜索」）。

**后端是实测选出来的，不是按名气挑的。**
在这台机器、这个网络里逐个打过（见任务记录）：

    Bing RSS     HTTP 200 / 854ms   <- 选它
    cn.bing.com  HTTP 200           <- 能通，但是 HTML，解析脆，只作参考
    DuckDuckGo   连不上（ConnectTimeout / DNS 失败）
    百度         弹「百度安全验证」，拿不到结果
    维基百科     超时

所以主路径用 Bing 的 **RSS 输出**（`format=rss`）：结构化 XML、不需要 Key、
不像 HTML 那样一改版就整体崩掉。

为什么不做成「模型自己调工具」的循环： 规定模型一次只返回一个 JSON、
由服务端解析后执行。这里沿用同一模式 —— 模型在 JSON 里填 `search_query`，
服务端搜完把结果喂回去让它**再答一次**，最多一轮。不引入新的协议概念。
"""
from __future__ import annotations

import html
import logging
import re

import httpx

logger = logging.getLogger("web_search")

# Bing 的 RSS 入口。加 ?format=rss 就返回 XML 而不是网页。
BING_SEARCH_URL = "https://www.bing.com/search"

REQUEST_TIMEOUT_SECONDS = 12.0

# 带一个常见的浏览器 UA。不带的话部分站点会直接返回 403/验证页。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

def _clean(text: str, *, limit: int = 300) -> str:
    """去掉 HTML 标签、还原实体、压掉多余空白，并截断。

    RSS 的 description 里带 `<strong>` 之类的标签（Bing 会把命中的关键词加粗），
    直接塞给模型会浪费 token 也干扰阅读，所以先洗一遍。
    """
    raw = html.unescape(text or "")
    raw = _TAG_RE.sub(" ", raw)
    raw = _WS_RE.sub(" ", raw).strip()
    return raw[:limit]

def parse_bing_rss(xml_text: str, *, limit: int = 5) -> list[dict[str, str]]:
    """把 Bing 的 RSS 输出解析成 `[{title,url,snippet}]`。

    纯函数，方便单测（不联网也能测解析）。解析不了就返回空列表 ——
    搜索是**加分项**，坏掉不能拖垮聊天链路。
    """
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(xml_text or "")
    except ElementTree.ParseError as exc:
        logger.warning("搜索结果不是合法 XML：%s", exc)
        return []

    out: list[dict[str, str]] = []
    for item in root.iter("item"):
        title = _clean(item.findtext("title") or "", limit=120)
        url = (item.findtext("link") or "").strip()
        snippet = _clean(item.findtext("description") or "")
        if not title and not snippet:
            continue
        out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= limit:
            break
    return out

async def search(query: str, *, limit: int = 5) -> list[dict[str, str]]:
    """搜一次，返回若干条结果。任何失败都返回空列表（不抛异常）。"""
    q = (query or "").strip()
    if not q:
        return []
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(
                BING_SEARCH_URL, params={"q": q, "format": "rss"}
            )
        if resp.status_code >= 400:
            logger.warning("搜索失败 HTTP %s（query=%r）", resp.status_code, q[:60])
            return []
        results = parse_bing_rss(resp.text, limit=limit)
        logger.info("搜索 %r 得到 %s 条结果", q[:60], len(results))
        return results
    except Exception as exc:  # noqa: BLE001 - 搜索失败绝不影响聊天
        logger.warning("搜索失败（query=%r）：%s", q[:60], exc)
        return []

def followup_text(query: str, results: list[dict[str, str]]) -> str:
    """把搜索结果包成一段「喂回给模型」的用户消息。

    关键在【现在怎么做】那几条 —— 尤其是**不要念网页**。
    模型很爱写「根据搜索结果」「资料显示」，那是助手口气，
    而用户要的是「朋友帮我查了一下」。
    """
    if not results:
        body = "（没搜到任何结果：可能网络不通，或关键词太生僻）"
    else:
        lines = []
        for index, item in enumerate(results, 1):
            lines.append(f"{index}. {item['title']}")
            if item.get("snippet"):
                lines.append(f"   {item['snippet']}")
            if item.get("url"):
                lines.append(f"   来源：{item['url']}")
        body = "\n".join(lines)
    return (
        "【联网搜索结果】\n"
        f"你刚才请求搜索：{query}\n\n"
        f"{body}\n\n"
        "【现在怎么做】\n"
        "- 用上面的结果回答用户刚才那句话，仍然按原来的 JSON 格式返回。\n"
        "- 这次 search_query 必须是 null（一次回复只搜一次）。\n"
        "- **像朋友转述，不要念网页**：别说「根据搜索结果」「资料显示」「综上所述」，\n"
        "  把你知道的直接当自己的话说出来。\n"
        "- 结果里没有的信息绝对不要编。没搜到就直说「我查了下没查到」，\n"
        "  或者按你自己的印象说，但要让人知道那是印象不是查到的。\n"
        "- 用户刚才要是只是闲聊、问感受、问看法，就忽略这些结果，正常回。"
    )

__all__: list[str] = [
    "search",
    "parse_bing_rss",
    "followup_text",
    "BING_SEARCH_URL",
    "REQUEST_TIMEOUT_SECONDS",
]
