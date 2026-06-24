#!/usr/bin/env python3
"""
今日头条文章排版 & 图片对齐预发布检查工具

在发布文章前通过 CDP 注入 JS，对编辑器中的图文内容进行全方位排版检查。
支持 CLI 独立运行，也可作为模块被 toutiao_publisher.py 调用。

检查项一览：
  1. 图片加载状态     — 是否有加载失败/占位图
  2. 图片尺寸合理性   — 过小/过大/被拉伸变形
  3. 图片对齐方式     — 居中/左对齐/浮动状态
  4. 图文重叠检测     — BoundingBox 碰撞检测
  5. 空白段落检测     — 连续空 <p> 或过大间距
  6. 内容溢出检测     — 内容是否超出编辑器宽度
  7. 图片 Alt 文本    — 是否缺失
  8. 标题与正文完整性  — 是否为空/过短
  9. 整体排版评分     — 加权汇总

使用方式:
  # CLI
  python layout_checker.py --port 9222

  # 带 JSON 输出（供其他 agent 消费）
  python layout_checker.py --port 9222 --json

  # Python API
  from layout_checker import LayoutChecker
  async with LayoutChecker(port=9222) as checker:
      report = await checker.run_all_checks()
"""

import asyncio
import json
import logging
import os
import sys
import time
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import websockets
except ImportError:
    print("请先安装依赖: pip install websockets")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("layout_checker")


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass
class Issue:
    """单条问题。"""
    severity: str         # "error" | "warning" | "info"
    category: str         # 检查类别
    message: str          # 描述
    element: str = ""     # 元素标识（选择器或索引）
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckReport:
    """完整检查报告。"""
    issues: list = field(default_factory=list)
    score: int = 100          # 排版评分 0-100
    summary: str = ""
    editor_info: dict = field(default_factory=dict)
    image_count: int = 0
    paragraph_count: int = 0
    checked_at: str = ""

    def add_issue(self, issue: Issue):
        self.issues.append(issue)
        # 扣分规则
        if issue.severity == "error":
            self.score -= 15
        elif issue.severity == "warning":
            self.score -= 5
        elif issue.severity == "info":
            self.score -= 1
        self.score = max(0, self.score)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "summary": self.summary,
            "editor_info": self.editor_info,
            "image_count": self.image_count,
            "paragraph_count": self.paragraph_count,
            "checked_at": self.checked_at,
            "issues": [i.to_dict() for i in self.issues],
        }


# ===========================================================================
# 轻量 CDP 客户端（自包含，不依赖 publisher）
# ===========================================================================
class _CDP:
    def __init__(self):
        self.ws = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listener = None

    async def connect(self, host="localhost", port=9222):
        import urllib.request
        url = f"http://{host}:{port}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
        page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        if not page:
            raise ConnectionError(f"在 {host}:{port} 未找到可用 page target")
        self.ws = await websockets.connect(
            page["webSocketDebuggerUrl"],
            max_size=50 * 1024 * 1024,
            ping_interval=30,
        )
        self._listener = asyncio.create_task(self._loop())
        await self.send("Runtime.enable")
        logger.info(f"已连接: {page.get('title', '')}")

    async def close(self):
        if self._listener:
            self._listener.cancel()
        if self.ws:
            await self.ws.close()

    async def send(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        self._pending[mid] = asyncio.get_event_loop().create_future()
        await self.ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(self._pending[mid], timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise

    async def _loop(self):
        try:
            async for raw in self.ws:
                m = json.loads(raw)
                mid = m.get("id")
                if mid and mid in self._pending:
                    f = self._pending.pop(mid)
                    if not f.done():
                        f.set_exception(Exception(m["error"]["message"])) if "error" in m else f.set_result(m.get("result", {}))
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass

    async def eval_js(self, expr, await_promise=False):
        r = await self.send("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        return r.get("result", {}).get("value")


# ===========================================================================
# 核心检查器
# ===========================================================================
class LayoutChecker:
    """
    排版检查器。

    通过 CDP 注入 JavaScript 到页面中，采集编辑器内图文元素的几何信息、
    样式信息和 DOM 结构，然后在 Python 侧进行分析并生成报告。
    """

    # ---- 可调阈值 ----
    THRESHOLDS = {
        "img_min_width": 80,           # 图片最小宽度 (px)
        "img_min_height": 60,          # 图片最小高度
        "img_max_width_ratio": 1.05,   # 图片宽度不超过编辑器宽度的 105%
        "img_aspect_ratio_diff": 0.15, # 拉伸变形阈值（宽高比偏差 >15%）
        "overlap_px": 5,               # 图文重叠容忍像素
        "max_empty_paragraphs": 2,     # 连续空段落上限
        "min_content_length": 50,      # 正文最少字符数
        "content_overflow_px": 10,     # 内容超出编辑器宽度容忍值
    }

    # 编辑器选择器（按优先级尝试）
    EDITOR_SELECTORS = [
        ".ProseMirror",
        '[contenteditable="true"]',
        ".ql-editor",
        ".editor-content",
        ".bf-content",
    ]

    TITLE_SELECTORS = [
        'textarea[placeholder*="标题"]',
        'input[placeholder*="标题"]',
        ".article-title textarea",
        ".article-title input",
        '[class*="title"] textarea',
        '[class*="title"] input',
    ]

    def __init__(self, host="localhost", port=9222):
        self.host = host
        self.port = port
        self.cdp = _CDP()

    async def __aenter__(self):
        await self.cdp.connect(self.host, self.port)
        return self

    async def __aexit__(self, *a):
        await self.cdp.close()

    # ------------------------------------------------------------------
    # 数据采集：从浏览器获取编辑器内的元素信息
    # ------------------------------------------------------------------
    async def _collect_editor_data(self) -> dict:
        """注入 JS 采集编辑器内所有图文元素的几何和样式信息。"""
        js = f"""
        (function() {{
            // 找编辑器
            var editor = null;
            var editorSelectors = {json.dumps(self.EDITOR_SELECTORS)};
            for (var i = 0; i < editorSelectors.length; i++) {{
                editor = document.querySelector(editorSelectors[i]);
                if (editor) break;
            }}
            if (!editor) return {{ error: '未找到编辑器元素' }};

            var editorRect = editor.getBoundingClientRect();
            var editorStyle = window.getComputedStyle(editor);

            // 粘贴上传残留特征：加载占位容器 / "编辑搜图"文本
            function _isUploadResidue(el) {{
                // 自身或其祖先节点命中已知残留类名
                var node = el;
                while (node && node !== editor) {{
                    var cn = node.className;
                    if (typeof cn === 'string' && (
                        cn.indexOf('img-loading-container') >= 0 ||
                        cn.indexOf('upload-residue') >= 0
                    )) return true;
                    node = node.parentElement;
                }}
                return false;
            }}

            function _hasResidueText(el) {{
                var t = (el.textContent || '').trim();
                return t === '编辑搜图' || t.indexOf('请点击编辑') === 0;
            }}

            // 采集所有图片（跳过粘贴残留容器内的占位图 + 零尺寸inline副本）
            var images = [];
            var imgs = editor.querySelectorAll('img');
            for (var i = 0; i < imgs.length; i++) {{
                var img = imgs[i];
                if (_isUploadResidue(img)) continue;
                var rect = img.getBoundingClientRect();
                // 跳过 inline 显示的零尺寸副本（编辑器贴图产生的重复元素）
                if (rect.width < 2 && rect.height < 2 &&
                    window.getComputedStyle(img).display === 'inline' &&
                    img.naturalWidth > 0) continue;
                var style = window.getComputedStyle(img);
                var parent = img.parentElement;
                var parentRect = parent ? parent.getBoundingClientRect() : null;
                var parentStyle = parent ? window.getComputedStyle(parent) : null;

                images.push({{
                    index: i,
                    src: (img.src || '').substring(0, 200),
                    alt: img.alt || '',
                    naturalWidth: img.naturalWidth,
                    naturalHeight: img.naturalHeight,
                    displayWidth: rect.width,
                    displayHeight: rect.height,
                    top: rect.top,
                    left: rect.left,
                    right: rect.right,
                    bottom: rect.bottom,
                    complete: img.complete,
                    hasError: img.complete && img.naturalWidth === 0,
                    display: style.display,
                    float: style.cssFloat || style.styleFloat || '',
                    objectFit: style.objectFit || '',
                    margin: style.margin,
                    textAlign: parentStyle ? parentStyle.textAlign : '',
                    parentTag: parent ? parent.tagName : '',
                    parentClass: parent ? parent.className : '',
                    parentDisplay: parentStyle ? parentStyle.display : '',
                    parentTextAlign: parentStyle ? parentStyle.textAlign : '',
                    isCentered: (function() {{
                        if (!parent || !parentRect) return false;
                        var imgCenter = rect.left + rect.width / 2;
                        var parentCenter = parentRect.left + parentRect.width / 2;
                        return Math.abs(imgCenter - parentCenter) < 10;
                    }})(),
                    overflowsEditor: rect.right > (editorRect.right + 5) || rect.left < (editorRect.left - 5),
                }});
            }}

            // 采集所有段落（跳过上传残留容器）
            var paragraphs = [];
            var ps = editor.querySelectorAll('p, div, h1, h2, h3, h4, h5, h6, blockquote, li');
            for (var i = 0; i < ps.length; i++) {{
                var p = ps[i];
                if (_isUploadResidue(p) || _hasResidueText(p)) continue;
                var rect = p.getBoundingClientRect();
                var style = window.getComputedStyle(p);
                var text = (p.textContent || '').trim();
                var hasImage = p.querySelector('img') !== null;

                paragraphs.push({{
                    index: i,
                    tag: p.tagName,
                    text: text.substring(0, 100),
                    textLength: text.length,
                    isEmpty: text.length === 0 && !hasImage,
                    hasImage: hasImage,
                    top: rect.top,
                    bottom: rect.bottom,
                    left: rect.left,
                    right: rect.right,
                    width: rect.width,
                    height: rect.height,
                    overflowsEditor: rect.right > (editorRect.right + 5),
                    marginTop: parseFloat(style.marginTop) || 0,
                    marginBottom: parseFloat(style.marginBottom) || 0,
                    paddingTop: parseFloat(style.paddingTop) || 0,
                    paddingBottom: parseFloat(style.paddingBottom) || 0,
                    lineHeight: parseFloat(style.lineHeight) || 0,
                    fontSize: parseFloat(style.fontSize) || 0,
                }});
            }}

            // 采集标题
            var titleValue = '';
            var titleSelectors = {json.dumps(self.TITLE_SELECTORS)};
            for (var i = 0; i < titleSelectors.length; i++) {{
                var tel = document.querySelector(titleSelectors[i]);
                if (tel) {{
                    titleValue = tel.value || tel.textContent || tel.innerText || '';
                    break;
                }}
            }}

            return {{
                editorRect: {{
                    top: editorRect.top,
                    left: editorRect.left,
                    right: editorRect.right,
                    bottom: editorRect.bottom,
                    width: editorRect.width,
                    height: editorRect.height,
                }},
                editorPadding: {{
                    left: parseFloat(editorStyle.paddingLeft) || 0,
                    right: parseFloat(editorStyle.paddingRight) || 0,
                }},
                contentWidth: editorRect.width
                    - (parseFloat(editorStyle.paddingLeft) || 0)
                    - (parseFloat(editorStyle.paddingRight) || 0),
                images: images,
                paragraphs: paragraphs,
                title: titleValue.trim(),
                totalTextLength: editor.textContent.trim().length,
                editorHtml: editor.innerHTML.substring(0, 500),
            }};
        }})()
        """
        return await self.cdp.eval_js(js)

    # ------------------------------------------------------------------
    # 检查项实现
    # ------------------------------------------------------------------
    def _check_images_loaded(self, data: dict, report: CheckReport):
        """检查1：图片加载状态。"""
        for img in data.get("images", []):
            if img.get("hasError"):
                report.add_issue(Issue(
                    severity="error",
                    category="图片加载",
                    message=f"第 {img['index']+1} 张图片加载失败",
                    element=f"img[{img['index']}]",
                    detail={"src": img.get("src", "")},
                ))
            elif not img.get("complete"):
                report.add_issue(Issue(
                    severity="warning",
                    category="图片加载",
                    message=f"第 {img['index']+1} 张图片尚未加载完成",
                    element=f"img[{img['index']}]",
                ))
            elif img.get("displayWidth", 0) < 2 or img.get("displayHeight", 0) < 2:
                report.add_issue(Issue(
                    severity="error",
                    category="图片加载",
                    message=f"第 {img['index']+1} 张图片显示尺寸为零（可能被隐藏）",
                    element=f"img[{img['index']}]",
                    detail={"width": img["displayWidth"], "height": img["displayHeight"]},
                ))

    def _check_image_sizes(self, data: dict, report: CheckReport):
        """检查2：图片尺寸合理性。"""
        t = self.THRESHOLDS
        content_w = data.get("contentWidth", 800)

        for img in data.get("images", []):
            w = img.get("displayWidth", 0)
            h = img.get("displayHeight", 0)
            nw = img.get("naturalWidth", 0)
            nh = img.get("naturalHeight", 0)

            # 过小
            if w < t["img_min_width"] and w > 0:
                report.add_issue(Issue(
                    severity="warning",
                    category="图片尺寸",
                    message=f"第 {img['index']+1} 张图片显示宽度过小 ({w:.0f}px < {t['img_min_width']}px)",
                    element=f"img[{img['index']}]",
                    detail={"displayWidth": w, "displayHeight": h, "naturalWidth": nw},
                ))

            # 超出编辑器
            if w > content_w * t["img_max_width_ratio"]:
                report.add_issue(Issue(
                    severity="error",
                    category="图片尺寸",
                    message=f"第 {img['index']+1} 张图片超出编辑器宽度 ({w:.0f}px > {content_w:.0f}px)",
                    element=f"img[{img['index']}]",
                    detail={"displayWidth": w, "contentWidth": content_w},
                ))

            # 拉伸变形
            if nw > 0 and nh > 0 and w > 0 and h > 0:
                natural_ratio = nw / nh
                display_ratio = w / h
                if natural_ratio > 0:
                    diff = abs(display_ratio - natural_ratio) / natural_ratio
                    if diff > t["img_aspect_ratio_diff"]:
                        report.add_issue(Issue(
                            severity="warning",
                            category="图片变形",
                            message=f"第 {img['index']+1} 张图片可能被拉伸变形 (偏差 {diff*100:.1f}%)",
                            element=f"img[{img['index']}]",
                            detail={
                                "naturalRatio": round(natural_ratio, 3),
                                "displayRatio": round(display_ratio, 3),
                                "diffPercent": round(diff * 100, 1),
                            },
                        ))

    def _check_image_alignment(self, data: dict, report: CheckReport):
        """检查3：图片对齐方式。"""
        for img in data.get("images", []):
            is_centered = img.get("isCentered", False)
            parent_align = img.get("parentTextAlign", "")
            float_val = img.get("float", "")

            # 判断对齐方式
            alignment = "未知"
            if is_centered or parent_align in ("center", "center"):
                alignment = "居中"
            elif parent_align in ("left", "right"):
                alignment = f"{parent_align}对齐"
            elif float_val and float_val != "none":
                alignment = f"浮动({float_val})"

            # 非居中的图片给 info 提示（头条文章通常居中更好看）
            if not is_centered and parent_align not in ("center",) and float_val in ("", "none"):
                report.add_issue(Issue(
                    severity="info",
                    category="图片对齐",
                    message=f"第 {img['index']+1} 张图片未居中 (当前: {alignment})",
                    element=f"img[{img['index']}]",
                    detail={
                        "parentTextAlign": parent_align,
                        "float": float_val,
                        "parentTag": img.get("parentTag", ""),
                    },
                ))

    def _check_overlap(self, data: dict, report: CheckReport):
        """检查4：图文重叠检测（基于 BoundingBox 碰撞）。"""
        t = self.THRESHOLDS
        images = data.get("images", [])
        paragraphs = data.get("paragraphs", [])
        tol = t["overlap_px"]

        for img in images:
            for para in paragraphs:
                if para.get("hasImage"):
                    continue  # 包含图片的段落本身不算重叠

                # BoundingBox 碰撞检测
                h_overlap = (
                    img["left"] - tol < para["right"]
                    and img["right"] + tol > para["left"]
                )
                v_overlap = (
                    img["top"] - tol < para["bottom"]
                    and img["bottom"] + tol > para["top"]
                )

                if h_overlap and v_overlap:
                    # 计算重叠面积
                    ox = max(0, min(img["right"], para["right"]) - max(img["left"], para["left"]))
                    oy = max(0, min(img["bottom"], para["bottom"]) - max(img["top"], para["top"]))
                    overlap_area = ox * oy

                    if overlap_area > 100:  # 重叠面积超过 100px² 才报警
                        report.add_issue(Issue(
                            severity="error",
                            category="图文重叠",
                            message=f"第 {img['index']+1} 张图片与第 {para['index']+1} 个段落存在重叠 ({overlap_area:.0f}px²)",
                            element=f"img[{img['index']}] vs p[{para['index']}]",
                            detail={
                                "overlapArea": round(overlap_area),
                                "paraText": para.get("text", "")[:50],
                            },
                        ))

    def _check_empty_paragraphs(self, data: dict, report: CheckReport):
        """检查5：连续空段落。"""
        t = self.THRESHOLDS
        paragraphs = data.get("paragraphs", [])
        consecutive_empty = 0
        start_index = None

        for p in paragraphs:
            if p.get("isEmpty"):
                if consecutive_empty == 0:
                    start_index = p["index"]
                consecutive_empty += 1
            else:
                if consecutive_empty > t["max_empty_paragraphs"]:
                    report.add_issue(Issue(
                        severity="warning",
                        category="空白段落",
                        message=f"发现 {consecutive_empty} 个连续空段落 (第 {start_index+1}-{start_index+consecutive_empty} 段)",
                        element=f"段落 {start_index+1}~{start_index+consecutive_empty}",
                        detail={"count": consecutive_empty},
                    ))
                consecutive_empty = 0

        # 尾部空段落
        if consecutive_empty > t["max_empty_paragraphs"]:
            report.add_issue(Issue(
                severity="warning",
                category="空白段落",
                message=f"文章尾部有 {consecutive_empty} 个连续空段落",
                element=f"段落 {start_index+1}~{start_index+consecutive_empty}",
            ))

    def _check_content_overflow(self, data: dict, report: CheckReport):
        """检查6：内容溢出编辑器。"""
        t = self.THRESHOLDS
        editor_rect = data.get("editorRect", {})
        editor_right = editor_rect.get("right", 0)
        editor_left = editor_rect.get("left", 0)

        for p in data.get("paragraphs", []):
            if p.get("overflowsEditor") and p.get("textLength", 0) > 0:
                overflow_right = max(0, p["right"] - editor_right)
                overflow_left = max(0, editor_left - p["left"])
                overflow = max(overflow_right, overflow_left)
                if overflow > t["content_overflow_px"]:
                    report.add_issue(Issue(
                        severity="warning",
                        category="内容溢出",
                        message=f"第 {p['index']+1} 段内容超出编辑器边界 ({overflow:.0f}px)",
                        element=f"p[{p['index']}]",
                        detail={"overflowPx": round(overflow), "text": p.get("text", "")[:50]},
                    ))

        for img in data.get("images", []):
            if img.get("overflowsEditor"):
                overflow_right = max(0, img["right"] - editor_right)
                overflow_left = max(0, editor_left - img["left"])
                overflow = max(overflow_right, overflow_left)
                if overflow > t["content_overflow_px"]:
                    report.add_issue(Issue(
                        severity="warning",
                        category="内容溢出",
                        message=f"第 {img['index']+1} 张图片超出编辑器边界 ({overflow:.0f}px)",
                        element=f"img[{img['index']}]",
                        detail={"overflowPx": round(overflow)},
                    ))

    def _check_image_alt(self, data: dict, report: CheckReport):
        """检查7：图片 alt 文本。"""
        for img in data.get("images", []):
            if not img.get("alt"):
                report.add_issue(Issue(
                    severity="info",
                    category="图片Alt",
                    message=f"第 {img['index']+1} 张图片缺少 alt 文本（影响可访问性和 SEO）",
                    element=f"img[{img['index']}]",
                ))

    def _check_title_and_content(self, data: dict, report: CheckReport):
        """检查8：标题和正文完整性。"""
        t = self.THRESHOLDS

        title = data.get("title", "")
        if not title:
            report.add_issue(Issue(
                severity="error",
                category="标题",
                message="文章标题为空",
            ))
        elif len(title) < 4:
            report.add_issue(Issue(
                severity="warning",
                category="标题",
                message=f"标题过短（{len(title)} 字），建议至少 4 个字",
                detail={"title": title},
            ))
        elif len(title) > 30:
            report.add_issue(Issue(
                severity="info",
                category="标题",
                message=f"标题较长（{len(title)} 字），头条推荐标题长度 5-30 字",
                detail={"title": title[:50]},
            ))

        total_len = data.get("totalTextLength", 0)
        if total_len < t["min_content_length"]:
            report.add_issue(Issue(
                severity="warning",
                category="正文",
                message=f"正文内容较短（{total_len} 字），建议不少于 {t['min_content_length']} 字",
            ))

    def _check_image_spacing(self, data: dict, report: CheckReport):
        """检查9：图片之间的间距是否合理。"""
        images = data.get("images", [])
        if len(images) < 2:
            return

        # 按 vertical position 排序
        sorted_imgs = sorted(images, key=lambda x: x.get("top", 0))
        for i in range(len(sorted_imgs) - 1):
            gap = sorted_imgs[i + 1]["top"] - sorted_imgs[i]["bottom"]
            if gap < 0:
                report.add_issue(Issue(
                    severity="warning",
                    category="图片间距",
                    message=f"第 {sorted_imgs[i]['index']+1} 和第 {sorted_imgs[i+1]['index']+1} 张图片之间无间距 (重叠 {abs(gap):.0f}px)",
                    element=f"img[{sorted_imgs[i]['index']}] ~ img[{sorted_imgs[i+1]['index']}]",
                    detail={"gapPx": round(gap)},
                ))
            elif gap < 8 and gap >= 0:
                report.add_issue(Issue(
                    severity="info",
                    category="图片间距",
                    message=f"第 {sorted_imgs[i]['index']+1} 和第 {sorted_imgs[i+1]['index']+1} 张图片间距过小 ({gap:.0f}px)",
                    element=f"img[{sorted_imgs[i]['index']}] ~ img[{sorted_imgs[i+1]['index']}]",
                    detail={"gapPx": round(gap)},
                ))

    def _check_text_around_images(self, data: dict, report: CheckReport):
        """检查10：图片周围是否有足够的文字分隔。"""
        images = data.get("images", [])
        paragraphs = data.get("paragraphs", [])

        for img in images:
            # 找图片上方和下方最近的段落
            above_paras = [p for p in paragraphs if p["bottom"] <= img["top"] + 5 and p.get("textLength", 0) > 0]
            below_paras = [p for p in paragraphs if p["top"] >= img["bottom"] - 5 and p.get("textLength", 0) > 0]

            if above_paras:
                closest_above = max(above_paras, key=lambda p: p["bottom"])
                gap = img["top"] - closest_above["bottom"]
                if gap < 5 and gap >= 0:
                    report.add_issue(Issue(
                        severity="info",
                        category="图文间距",
                        message=f"第 {img['index']+1} 张图片与上方文字间距过小 ({gap:.0f}px)",
                        element=f"img[{img['index']}]",
                        detail={"gapPx": round(gap)},
                    ))

            if below_paras:
                closest_below = min(below_paras, key=lambda p: p["top"])
                gap = closest_below["top"] - img["bottom"]
                if gap < 5 and gap >= 0:
                    report.add_issue(Issue(
                        severity="info",
                        category="图文间距",
                        message=f"第 {img['index']+1} 张图片与下方文字间距过小 ({gap:.0f}px)",
                        element=f"img[{img['index']}]",
                        detail={"gapPx": round(gap)},
                    ))

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    async def run_all_checks(self) -> CheckReport:
        """运行全部检查，返回 CheckReport。"""
        report = CheckReport()
        report.checked_at = time.strftime("%Y-%m-%d %H:%M:%S")

        # 采集数据
        logger.info("正在采集编辑器排版数据...")
        data = await self._collect_editor_data()

        if data.get("error"):
            report.add_issue(Issue(
                severity="error",
                category="编辑器",
                message=data["error"],
            ))
            report.summary = "无法执行检查：" + data["error"]
            return report

        # 基本信息
        report.editor_info = data.get("editorRect", {})
        report.image_count = len(data.get("images", []))
        report.paragraph_count = len(data.get("paragraphs", []))

        logger.info(f"采集完成: {report.image_count} 张图片, {report.paragraph_count} 个段落")
        logger.info(f"编辑器宽度: {data.get('contentWidth', 0):.0f}px, "
                     f"总文字: {data.get('totalTextLength', 0)} 字")

        # 依次执行检查
        self._check_images_loaded(data, report)
        self._check_image_sizes(data, report)
        self._check_image_alignment(data, report)
        self._check_overlap(data, report)
        self._check_empty_paragraphs(data, report)
        self._check_content_overflow(data, report)
        self._check_image_alt(data, report)
        self._check_title_and_content(data, report)
        self._check_image_spacing(data, report)
        self._check_text_around_images(data, report)

        # 生成摘要
        errors = sum(1 for i in report.issues if i.severity == "error")
        warnings = sum(1 for i in report.issues if i.severity == "warning")
        infos = sum(1 for i in report.issues if i.severity == "info")

        if errors == 0 and warnings == 0:
            report.summary = f"排版良好！评分 {report.score}/100，仅有 {infos} 条优化建议"
        elif errors == 0:
            report.summary = f"基本合格，评分 {report.score}/100，{warnings} 个警告，{infos} 条建议"
        else:
            report.summary = f"存在问题，评分 {report.score}/100，{errors} 个错误需修复"

        return report


# ===========================================================================
# 报告格式化输出
# ===========================================================================
def format_report_text(report: CheckReport) -> str:
    """将报告格式化为可读的纯文本。"""
    lines = []
    lines.append("=" * 60)
    lines.append("  今日头条文章排版检查报告")
    lines.append("=" * 60)
    lines.append(f"  检查时间: {report.checked_at}")
    lines.append(f"  评分: {report.score}/100")
    lines.append(f"  图片数: {report.image_count}  |  段落数: {report.paragraph_count}")
    lines.append(f"  摘要: {report.summary}")
    lines.append("-" * 60)

    if not report.issues:
        lines.append("  没有发现任何问题，排版良好！")
    else:
        # 按严重程度分组
        for severity, label, icon in [
            ("error", "错误（必须修复）", "✗"),
            ("warning", "警告（建议修复）", "!"),
            ("info", "建议（可选优化）", "·"),
        ]:
            issues = [i for i in report.issues if i.severity == severity]
            if not issues:
                continue
            lines.append(f"\n  [{label}]")
            for idx, issue in enumerate(issues, 1):
                lines.append(f"  {icon} {idx}. [{issue.category}] {issue.message}")
                if issue.element:
                    lines.append(f"      位置: {issue.element}")
                if issue.detail:
                    detail_str = json.dumps(issue.detail, ensure_ascii=False)
                    if len(detail_str) > 100:
                        detail_str = detail_str[:100] + "..."
                    lines.append(f"      详情: {detail_str}")

    lines.append("\n" + "=" * 60)

    # 修复建议
    errors = [i for i in report.issues if i.severity == "error"]
    if errors:
        lines.append("\n  修复建议:")
        categories = set(i.category for i in errors)
        if "图片加载" in categories:
            lines.append("  - 加载失败的图片：检查图片 URL 是否正确，或重新上传")
        if "图片尺寸" in categories:
            lines.append("  - 尺寸异常的图片：调整图片大小或 CSS 样式")
        if "图文重叠" in categories:
            lines.append("  - 图文重叠：检查图片的 display/float 属性，确保图文不碰撞")
        if "标题" in categories:
            lines.append("  - 标题问题：填写一个 5-30 字的标题")

    lines.append("")
    return "\n".join(lines)


# ===========================================================================
# CLI
# ===========================================================================
def main():
    parser = argparse.ArgumentParser(
        description="今日头条文章排版 & 图片对齐预发布检查工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 运行检查并输出可读报告
  python layout_checker.py

  # JSON 格式输出（供其他 agent 解析）
  python layout_checker.py --json

  # 指定端口
  python layout_checker.py --port 9223

  # 同时输出可读报告和 JSON
  python layout_checker.py --json --verbose

退出码:
  0 = 无错误
  1 = 存在错误
  2 = 连接失败
        """,
    )
    parser.add_argument("--host", default="localhost", help="CDP 地址 (默认 localhost)")
    parser.add_argument("--port", type=int, default=9222, help="CDP 端口 (默认 9222)")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--verbose", action="store_true", help="同时输出文本和 JSON")
    parser.add_argument("--output", "-o", help="将报告写入文件")

    args = parser.parse_args()

    async def _run():
        try:
            async with LayoutChecker(host=args.host, port=args.port) as checker:
                report = await checker.run_all_checks()

                if args.verbose:
                    print(format_report_text(report))
                    print("\n--- JSON ---")
                    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                elif args.json:
                    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
                else:
                    print(format_report_text(report))

                if args.output:
                    with open(args.output, "w", encoding="utf-8") as f:
                        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
                    logger.info(f"报告已写入: {args.output}")

                # 退出码
                has_errors = any(i.severity == "error" for i in report.issues)
                sys.exit(1 if has_errors else 0)

        except ConnectionError as e:
            logger.error(str(e))
            sys.exit(2)
        except Exception as e:
            logger.error(f"检查失败: {e}")
            sys.exit(2)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
