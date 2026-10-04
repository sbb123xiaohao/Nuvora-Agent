"""联网工具：DuckDuckGo 搜索 + 网页正文提取。"""

from __future__ import annotations

from ..config import Config


def do_web_search(cfg: Config, query: str, max_results: int | None = None) -> str:
    if not query.strip():
        return "（搜索词为空）"
    limit = max_results or cfg.tools.web_search_max_results
    try:
        from ddgs import DDGS
    except ImportError:
        return "（web_search 依赖未安装：pip install ddgs）"
    try:
        with DDGS(timeout=20) as ddgs:
            rows = ddgs.text(query, max_results=limit)
    except Exception as e:  # noqa: BLE001 —— 工具失败要以字符串形式回给模型
        return f"搜索失败：{type(e).__name__}: {e}（可稍后重试或更换关键词）"
    if not rows:
        return "没有找到相关结果，建议更换或简化关键词后重试。"
    lines = []
    for i, row in enumerate(rows, 1):
        title = row.get("title") or ""
        href = row.get("href") or row.get("url") or ""
        body = row.get("body") or row.get("description") or ""
        lines.append(f"{i}. {title}\n   链接: {href}\n   摘要: {body}")
    return "\n\n".join(lines)


def do_web_fetch(url: str, max_chars: int = 8000) -> str:
    if not url.strip().lower().startswith(("http://", "https://")):
        return "（仅支持 http/https 链接）"
    import httpx

    try:
        import trafilatura

        have_trafilatura = True
    except ImportError:
        have_trafilatura = False

    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) NUVORA/0.1 (+agent)"}
    try:
        resp = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        return f"抓取失败：{type(e).__name__}: {e}"
    html = resp.text
    text = None
    if have_trafilatura:
        try:
            text = trafilatura.extract(html, output_format="markdown", include_comments=False)
        except Exception:  # noqa: BLE001
            text = None
        if not text:
            try:
                text = trafilatura.extract(html)
            except Exception:  # noqa: BLE001
                text = None
    if not text:
        text = html
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n…（已截断，全文约 {len(text)} 字符）"
    return text or "未能从该页面提取到正文。"
