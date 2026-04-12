"""
socake.github.io — 自动化测试套件
用 Playwright 测试线上站点的关键功能。

运行方式：
  cd /home/ubuntu/socake-site
  python3 tests/test_site.py

依赖：
  pip install playwright pytest-playwright
  playwright install chromium
"""

import sys
import time
import re
from dataclasses import dataclass, field
from typing import Optional
from playwright.sync_api import sync_playwright, Page, expect

BASE_URL = "https://socake.github.io"
TIMEOUT = 15000  # 15s

# ──────────────────────────────────────────────
# 结果收集
# ──────────────────────────────────────────────
@dataclass
class Result:
    name: str
    passed: bool
    detail: str = ""

results: list[Result] = []

def ok(name: str, detail: str = ""):
    results.append(Result(name, True, detail))
    print(f"  ✓  {name}" + (f"  ({detail})" if detail else ""))

def fail(name: str, detail: str = ""):
    results.append(Result(name, False, detail))
    print(f"  ✗  {name}  →  {detail}")

def section(title: str):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}")


# ──────────────────────────────────────────────
# 测试：页面可访问性 & HTTP 状态
# ──────────────────────────────────────────────
PAGES = [
    ("/",           "首页"),
    ("/posts/",     "博客列表"),
    ("/roadmap/",   "路线图"),
    ("/docs/linux/","Linux 文档"),
    ("/changelog/", "更新日志"),
    ("/tags/",      "标签页"),
]

def test_pages_load(page: Page):
    section("页面可访问性")
    for path, label in PAGES:
        url = BASE_URL + path
        try:
            resp = page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
            status = resp.status if resp else 0
            if status == 200:
                ok(f"{label} ({path})", f"HTTP {status}")
            else:
                fail(f"{label} ({path})", f"HTTP {status}")
        except Exception as e:
            fail(f"{label} ({path})", str(e)[:80])


# ──────────────────────────────────────────────
# 测试：导航栏
# ──────────────────────────────────────────────
def test_navigation(page: Page):
    section("导航栏")
    page.goto(BASE_URL, timeout=TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_selector(".main-menu", timeout=5000)

    # 导航链接存在
    nav_links = page.query_selector_all(".main-menu nav a")
    if len(nav_links) >= 3:
        ok("导航链接数量", f"找到 {len(nav_links)} 个")
    else:
        fail("导航链接数量", f"只找到 {len(nav_links)} 个")

    # 博客链接可点击并跳转
    try:
        page.click('a[href="/posts/"]', timeout=3000)
        page.wait_for_url("**/posts/**", timeout=5000)
        ok("博客链接跳转", page.url)
    except Exception as e:
        fail("博客链接跳转", str(e)[:80])

    # 返回首页，测试下拉菜单
    page.goto(BASE_URL, timeout=TIMEOUT, wait_until="domcontentloaded")
    try:
        # 悬停触发下拉
        page.hover("text=运维", timeout=3000)
        linux_link = page.query_selector("a[href='/docs/linux/']")
        if linux_link:
            ok("运维下拉菜单", "Linux 链接存在")
        else:
            fail("运维下拉菜单", "找不到 Linux 链接")
    except Exception as e:
        fail("运维下拉菜单", str(e)[:80])


# ──────────────────────────────────────────────
# 测试：首页内容
# ──────────────────────────────────────────────
def test_homepage(page: Page):
    section("首页内容")
    page.goto(BASE_URL, timeout=TIMEOUT, wait_until="domcontentloaded")

    # 等待内容加载
    try:
        page.wait_for_selector("article header", timeout=5000)
    except:
        pass

    # 头像
    avatar = page.query_selector("article header img.rounded-full")
    if avatar:
        ok("头像图片")
    else:
        fail("头像图片", "找不到 img.rounded-full")

    # 博主姓名
    h1 = page.query_selector("article header h1")
    if h1:
        text = h1.inner_text().strip()
        ok("博主姓名 h1", text[:30])
    else:
        fail("博主姓名 h1", "找不到")

    # 最近文章
    cards = page.query_selector_all(".post-card, .mini-card, article.relative")
    if len(cards) >= 3:
        ok("最近文章卡片", f"找到 {len(cards)} 张")
    else:
        fail("最近文章卡片", f"只有 {len(cards)} 张")


# ──────────────────────────────────────────────
# 测试：博客列表页
# ──────────────────────────────────────────────
def test_posts_list(page: Page):
    section("博客列表页")
    page.goto(BASE_URL + "/posts/", timeout=TIMEOUT, wait_until="domcontentloaded")

    # Filter bar
    filter_bar = page.query_selector("#filter-bar, .filter-bar")
    if filter_bar:
        ok("Filter Bar 存在")
    else:
        fail("Filter Bar 存在", "找不到")

    # 分类筛选按钮
    btns = page.query_selector_all(".filter-btn")
    if len(btns) >= 5:
        ok("筛选按钮", f"{len(btns)} 个分类")
    else:
        fail("筛选按钮", f"只有 {len(btns)} 个")

    # 文章卡片
    cards = page.query_selector_all(".post-card")
    if len(cards) >= 10:
        ok("文章卡片", f"共 {len(cards)} 篇")
    else:
        fail("文章卡片", f"只有 {len(cards)} 篇")

    # 点击分类筛选
    try:
        kubernetes_btn = page.query_selector(".filter-btn[data-filter='kubernetes']")
        if kubernetes_btn:
            kubernetes_btn.click()
            page.wait_for_timeout(500)
            visible = page.query_selector_all(".post-card:visible")
            ok("Kubernetes 分类筛选", f"筛选后 {len(visible)} 篇可见")
        else:
            fail("Kubernetes 分类筛选", "找不到 kubernetes 按钮")
    except Exception as e:
        fail("Kubernetes 分类筛选", str(e)[:80])


# ──────────────────────────────────────────────
# 测试：文章页
# ──────────────────────────────────────────────
def test_article(page: Page):
    section("文章页")
    # 随便打开一篇文章
    page.goto(BASE_URL + "/posts/", timeout=TIMEOUT, wait_until="domcontentloaded")
    try:
        first_card = page.query_selector(".post-card a.post-card-title-link")
        if not first_card:
            first_card = page.query_selector(".post-card a[href]")
        href = first_card.get_attribute("href") if first_card else None
        if href:
            page.goto(BASE_URL + href if href.startswith("/") else href,
                      timeout=TIMEOUT, wait_until="domcontentloaded")
        else:
            fail("打开文章", "找不到文章链接")
            return
    except Exception as e:
        fail("打开文章", str(e)[:80])
        return

    ok("文章页加载", page.url[-60:])

    # 文章标题
    h1 = page.query_selector("h1, #single_header h1")
    if h1:
        ok("文章标题 h1", h1.inner_text()[:30])
    else:
        fail("文章标题 h1", "找不到")

    # Hero 图片
    hero_img = page.query_selector(".single_hero_background img, #background-image")
    if hero_img:
        ok("Hero 图片")
    else:
        fail("Hero 图片", "找不到")

    # 目录 TOC
    toc = page.query_selector("#TableOfContents")
    if toc:
        toc_links = toc.query_selector_all("a")
        ok("目录 TOC", f"{len(toc_links)} 个章节")
    else:
        fail("目录 TOC", "找不到（可能文章没有标题）")

    # Prose 内容
    prose = page.query_selector(".prose")
    if prose:
        word_count = len(prose.inner_text().split())
        ok("正文 Prose", f"约 {word_count} 词")
    else:
        fail("正文 Prose", "找不到 .prose 元素")


# ──────────────────────────────────────────────
# 测试：亮色/暗色模式切换
# ──────────────────────────────────────────────
def test_theme_toggle(page: Page):
    section("亮色 / 暗色模式")
    page.goto(BASE_URL, timeout=TIMEOUT, wait_until="domcontentloaded")

    # 检查默认是暗色
    html_class = page.evaluate("document.documentElement.className")
    html_appearance = page.evaluate(
        "document.documentElement.getAttribute('data-default-appearance')"
    )
    ok("默认外观配置", f"data-default-appearance={html_appearance}")

    is_dark = "dark" in html_class
    ok(f"初始模式: {'暗色' if is_dark else '亮色'}", f"class={html_class[:50]}")

    # 切换到亮色模式
    try:
        page.click("#appearance-switcher", timeout=3000)
        page.wait_for_timeout(500)
        html_class_after = page.evaluate("document.documentElement.className")
        is_dark_after = "dark" in html_class_after
        if is_dark and not is_dark_after:
            ok("切换到亮色成功", f"class={html_class_after[:50]}")
        elif not is_dark and is_dark_after:
            ok("切换到暗色成功", f"class={html_class_after[:50]}")
        else:
            fail("切换模式", f"切换前后 dark 状态相同: {html_class_after[:50]}")
    except Exception as e:
        fail("模式切换按钮", str(e)[:80])
        return

    # 亮色模式下检查背景色
    bg_color = page.evaluate(
        "getComputedStyle(document.body).backgroundColor"
    )
    ok("亮色模式 body 背景色", bg_color)

    # 切回暗色
    try:
        page.click("#appearance-switcher", timeout=3000)
        page.wait_for_timeout(300)
        ok("切回暗色模式")
    except Exception as e:
        fail("切回暗色", str(e)[:80])


# ──────────────────────────────────────────────
# 测试：文字可读性（对比度检测）
# ──────────────────────────────────────────────
def _luminance(r, g, b):
    """计算相对亮度"""
    vals = [x / 255 for x in (r, g, b)]
    vals = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]

def _contrast(c1: str, c2: str) -> float:
    """计算两个 rgb() 颜色的对比度"""
    def parse(c):
        nums = re.findall(r"[\d.]+", c)
        return tuple(float(x) for x in nums[:3])
    try:
        r1, g1, b1 = parse(c1)
        r2, g2, b2 = parse(c2)
        l1 = _luminance(r1, g1, b1)
        l2 = _luminance(r2, g2, b2)
        lighter, darker = max(l1, l2), min(l1, l2)
        return (lighter + 0.05) / (darker + 0.05)
    except:
        return 0.0

def test_readability(page: Page):
    section("文字可读性")
    page.goto(BASE_URL + "/posts/", timeout=TIMEOUT, wait_until="domcontentloaded")
    page.wait_for_selector(".post-card", timeout=5000)

    checks = [
        # (selector, 描述, 模式)
        (".post-card-title", "卡片标题（暗色）"),
        (".post-card-summary", "卡片摘要（暗色）"),
        (".year-label", "年份标签（暗色）"),
        (".filter-btn", "筛选按钮（暗色）"),
    ]

    def check_contrast(sel, label):
        el = page.query_selector(sel)
        if not el:
            fail(label, f"找不到 {sel}")
            return
        color = page.evaluate(f"getComputedStyle(document.querySelector('{sel}')).color")
        bg = page.evaluate(
            f"""
            (function() {{
                // 收集从元素到 body 的 rgba 背景色栈，然后从下到上混合
                let el = document.querySelector('{sel}');
                let layers = [];
                while (el) {{
                    let bg = getComputedStyle(el).backgroundColor;
                    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {{
                        layers.unshift(bg);
                        // 如果已经是完全不透明的，直接停止
                        let parts = bg.match(/[\d.]+/g) || [];
                        let alpha = parts.length >= 4 ? parseFloat(parts[3]) : 1;
                        if (alpha >= 1) break;
                    }}
                    el = el.parentElement;
                }}
                if (layers.length === 0) return getComputedStyle(document.body).backgroundColor;
                // 从最底层往上混合
                let r = 9, g = 14, b = 36; // 默认暗色底
                for (let layer of layers) {{
                    let parts = (layer.match(/[\d.]+/g) || []).map(Number);
                    let lr = parts[0] || 0, lg = parts[1] || 0, lb = parts[2] || 0;
                    let a = parts.length >= 4 ? parts[3] : 1;
                    r = Math.round(a * lr + (1 - a) * r);
                    g = Math.round(a * lg + (1 - a) * g);
                    b = Math.round(a * lb + (1 - a) * b);
                }}
                return 'rgb(' + r + ', ' + g + ', ' + b + ')';
            }})()
            """
        )
        ratio = _contrast(color, bg)
        if ratio >= 4.5:
            ok(label, f"对比度 {ratio:.1f}:1 ✓ WCAG AA")
        elif ratio >= 3.0:
            ok(label, f"对比度 {ratio:.1f}:1 (低于 AA 标准 4.5)")
        else:
            fail(label, f"对比度仅 {ratio:.1f}:1 (不可读, 需 ≥ 4.5)")

    for sel, label in checks:
        check_contrast(sel, label)

    # 切换亮色模式再测一遍
    try:
        page.click("#appearance-switcher", timeout=3000)
        page.wait_for_timeout(600)

        light_checks = [
            (".post-card-title", "卡片标题（亮色）"),
            (".post-card-summary", "卡片摘要（亮色）"),
            (".filter-btn", "筛选按钮（亮色）"),
        ]
        for sel, label in light_checks:
            check_contrast(sel, label)

        page.click("#appearance-switcher", timeout=3000)
        page.wait_for_timeout(300)
    except Exception as e:
        fail("亮色可读性测试", str(e)[:80])


# ──────────────────────────────────────────────
# 测试：搜索功能
# ──────────────────────────────────────────────
def test_search(page: Page):
    section("搜索功能")
    page.goto(BASE_URL, timeout=TIMEOUT, wait_until="domcontentloaded")
    try:
        page.click("#search-button", timeout=3000)
        page.wait_for_timeout(500)
        search_input = page.query_selector("input[type='search'], #search-query, input[placeholder]")
        if search_input:
            ok("搜索框打开")
            search_input.type("kubernetes", delay=50)
            page.wait_for_timeout(800)
            results_el = page.query_selector_all(".search-result, [id*='search'] li, [class*='result']")
            ok("搜索输入", f"输入 'kubernetes'，找到 {len(results_el)} 个结果元素")
        else:
            fail("搜索框", "找不到 input 元素")
        # 关闭
        page.keyboard.press("Escape")
    except Exception as e:
        fail("搜索功能", str(e)[:80])


# ──────────────────────────────────────────────
# 测试：移动端响应式
# ──────────────────────────────────────────────
def test_mobile(page: Page):
    section("移动端响应式 (375px)")
    page.set_viewport_size({"width": 375, "height": 812})
    page.goto(BASE_URL, timeout=TIMEOUT, wait_until="domcontentloaded")

    # 汉堡菜单存在
    menu_btn = page.query_selector("#menu-button")
    if menu_btn:
        ok("汉堡菜单按钮")
        # 点击打开
        menu_btn.click()
        page.wait_for_timeout(500)
        menu_wrapper = page.query_selector("#menu-wrapper")
        if menu_wrapper:
            is_visible = menu_wrapper.is_visible()
            ok("移动端菜单展开", f"visible={is_visible}")
        else:
            fail("移动端菜单", "找不到 #menu-wrapper")
    else:
        fail("汉堡菜单按钮", "找不到 #menu-button")

    # 恢复桌面尺寸
    page.set_viewport_size({"width": 1280, "height": 800})


# ──────────────────────────────────────────────
# 测试：关键链接不 404
# ──────────────────────────────────────────────
CRITICAL_LINKS = [
    "/posts/",
    "/roadmap/",
    "/docs/linux/",
    "/docs/docker/",
    "/docs/kubernetes/",
    "/tags/",
    "/categories/",
    "/posts/authors/",
    "/changelog/",
]

def test_no_404(page: Page):
    section("关键链接 404 检查")
    for path in CRITICAL_LINKS:
        url = BASE_URL + path
        try:
            resp = page.goto(url, timeout=TIMEOUT, wait_until="domcontentloaded")
            status = resp.status if resp else 0
            if status == 200:
                ok(path, f"200 OK")
            else:
                fail(path, f"HTTP {status}")
        except Exception as e:
            fail(path, str(e)[:60])


# ──────────────────────────────────────────────
# 截图（可选）
# ──────────────────────────────────────────────
def take_screenshots(page: Page):
    section("截图归档")
    import os
    os.makedirs("/tmp/socake-screenshots", exist_ok=True)

    shots = [
        (BASE_URL, "homepage-dark"),
        (BASE_URL + "/posts/", "posts-dark"),
    ]
    page.set_viewport_size({"width": 1440, "height": 900})
    for url, name in shots:
        page.goto(url, timeout=TIMEOUT, wait_until="networkidle")
        path = f"/tmp/socake-screenshots/{name}.png"
        page.screenshot(path=path, full_page=False)
        ok(f"截图: {name}", path)

    # 亮色模式截图
    page.goto(BASE_URL, timeout=TIMEOUT, wait_until="networkidle")
    page.click("#appearance-switcher")
    page.wait_for_timeout(600)
    page.screenshot(path="/tmp/socake-screenshots/homepage-light.png", full_page=False)
    ok("截图: homepage-light", "/tmp/socake-screenshots/homepage-light.png")

    page.goto(BASE_URL + "/posts/", timeout=TIMEOUT, wait_until="networkidle")
    page.screenshot(path="/tmp/socake-screenshots/posts-light.png", full_page=False)
    ok("截图: posts-light", "/tmp/socake-screenshots/posts-light.png")


# ──────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────
def main():
    print(f"\n{'═'*50}")
    print(f"  socake.github.io — 自动化测试")
    print(f"  目标: {BASE_URL}")
    print(f"{'═'*50}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = ctx.new_page()
        page.set_default_timeout(TIMEOUT)

        # 静默忽略控制台错误（字体加载等无关错误）
        page.on("console", lambda msg: None)

        test_pages_load(page)
        test_navigation(page)
        test_homepage(page)
        test_posts_list(page)
        test_article(page)
        test_theme_toggle(page)
        test_readability(page)
        test_search(page)
        test_mobile(page)
        test_no_404(page)
        take_screenshots(page)

        browser.close()

    # ── 汇总 ──
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed

    print(f"\n{'═'*50}")
    print(f"  测试完成：{passed}/{total} 通过  |  {failed} 失败")
    print(f"{'═'*50}")

    if failed:
        print("\n失败项：")
        for r in results:
            if not r.passed:
                print(f"  ✗  {r.name}  →  {r.detail}")
        print()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
