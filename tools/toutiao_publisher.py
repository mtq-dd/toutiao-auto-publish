#!/usr/bin/env python3
"""
今日头条图文文章 CDP 自动发布工具

通过 Chrome DevTools Protocol (CDP) 控制已登录的 Edge 浏览器，
自动完成文章的标题填写、正文插入、图片上传和发布操作。

文件上传核心方案（已验证有效）：
  1. 通过 ClipboardEvent + File 对象粘贴图片到 ProseMirror 编辑器
  2. 编辑器自动上传图片到头条服务器（image-tt-private.toutiao.com）
  3. 封面自动从正文图片中选取（三图模式）
  4. 发布流程：预览并发布 → 确认发布（两步）

依赖: pip install websockets

使用方式:
  # CLI
  python toutiao_publisher.py publish \
      --title "文章标题" \
      --content "文章正文（纯文本或HTML）" \
      --images cover.jpg img1.png img2.png \
      --port 9222

  # Python API
  from toutiao_publisher import ToutiaoPublisher
  async with ToutiaoPublisher(port=9222) as pub:
      await pub.publish_article(
          title="文章标题",
          content="<p>正文HTML</p>",
          image_paths=["cover.jpg"],
      )
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import argparse
from pathlib import Path
from typing import Optional

try:
    import websockets
except ImportError:
    print("请先安装依赖: pip install websockets")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("toutiao")


# ---------------------------------------------------------------------------
# CDP 客户端
# ---------------------------------------------------------------------------
class CDPClient:
    """轻量级 CDP 客户端，通过 WebSocket 与浏览器通信。"""

    def __init__(self):
        self.ws = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._event_handlers: dict[str, list] = {}
        self._listener_task: Optional[asyncio.Task] = None

    # -- 连接管理 --

    async def connect(self, host: str = "localhost", port: int = 9222) -> str:
        """连接到 CDP 调试端口，自动选取第一个 page 类型 target。"""
        import urllib.request

        url = f"http://{host}:{port}/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())

        page_target = None
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                page_target = t
                break

        if not page_target:
            raise ConnectionError(
                f"在 {host}:{port} 上没有找到可用的 page target。\n"
                "请确认 Edge 已用 --remote-debugging-port=9222 启动，且已打开页面。"
            )

        ws_url = page_target["webSocketDebuggerUrl"]
        logger.info(f"连接目标: {page_target.get('title', 'unknown')}")
        logger.info(f"WS URL: {ws_url}")

        self.ws = await websockets.connect(
            ws_url,
            max_size=50 * 1024 * 1024,  # 50MB，支持大响应
            ping_interval=30,
            ping_timeout=10,
        )
        self._listener_task = asyncio.create_task(self._listener_loop())

        # 启用必要的 CDP 域
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        try:
            await self.send("DOM.enable")
        except Exception:
            logger.warning("DOM.enable 失败（部分页面不支持），继续尝试")

        return ws_url

    async def close(self):
        """关闭 WebSocket 连接。"""
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self.ws:
            await self.ws.close()
            logger.info("CDP 连接已关闭")

    # -- 消息收发 --

    async def send(self, method: str, params: Optional[dict] = None, timeout: int = 30) -> dict:
        """发送 CDP 命令并等待响应。"""
        self._msg_id += 1
        msg_id = self._msg_id
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[msg_id] = future

        await self.ws.send(json.dumps(payload))

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"CDP 命令超时 ({timeout}s): {method}")

    def on_event(self, event_name: str, callback):
        """注册 CDP 事件回调（支持多个回调）。"""
        self._event_handlers.setdefault(event_name, []).append(callback)

    async def _listener_loop(self):
        """后台循环：接收 CDP 消息并分发。"""
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")

                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                Exception(
                                    f"CDP 错误: {msg['error'].get('message', msg['error'])}"
                                )
                            )
                        else:
                            fut.set_result(msg.get("result", {}))
                else:
                    # 事件分发
                    method = msg.get("method")
                    if method and method in self._event_handlers:
                        for cb in self._event_handlers[method]:
                            try:
                                ret = cb(msg.get("params", {}))
                                if asyncio.iscoroutine(ret):
                                    await ret
                            except Exception as e:
                                logger.warning(f"事件处理异常 [{method}]: {e}")
        except websockets.ConnectionClosed:
            logger.warning("CDP WebSocket 连接已断开")
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# 文件上传核心模块
# ---------------------------------------------------------------------------
class FileUploadHandler:
    """
    处理 CDP 文件上传的核心逻辑。

    关键步骤（顺序很重要！）：
      1. 开启 Page.setInterceptFileChooserDialog(true)  —— 拦截文件对话框
      2. 注册 Page.fileChooserOpened 事件回调
      3. 用 JS 找到并点击上传触发元素（可能是隐藏的 <input type="file">）
      4. 在 fileChooserOpened 回调中用 DOM.setFileInputFiles 注入文件
      5. 等待上传完成
    """

    def __init__(self, cdp: CDPClient):
        self.cdp = cdp
        self._chooser_event = asyncio.Event()
        self._chooser_backend_node_id: Optional[int] = None

    async def enable_interception(self):
        """启用文件选择器拦截。必须在任何上传操作之前调用。"""
        try:
            await self.cdp.send(
                "Page.setInterceptFileChooserDialog",
                {"enabled": True},
            )
            self.cdp.on_event("Page.fileChooserOpened", self._on_file_chooser_opened)
            logger.info("文件选择器拦截已启用")
        except Exception:
            logger.warning("文件选择器拦截不可用，将使用剪贴板粘贴方案")

    async def _on_file_chooser_opened(self, params: dict):
        """Page.fileChooserOpened 事件回调。"""
        self._chooser_backend_node_id = params.get("backendNodeId")
        mode = params.get("mode", "select")
        logger.info(f"文件选择器已打开 (mode={mode}, backendNodeId={self._chooser_backend_node_id})")
        self._chooser_event.set()

    async def upload_via_click(
        self,
        file_path: str,
        click_js: str,
        timeout: int = 30,
        wait_upload_js: Optional[str] = None,
    ) -> bool:
        """
        通过点击触发上传。

        Args:
            file_path: 要上传的本地文件绝对路径
            click_js:  点击上传按钮的 JavaScript 代码（需返回 true/false）
            timeout:   等待文件选择器打开的超时秒数
            wait_upload_js: 上传后等待完成的 JS（返回 true 表示完成）

        Returns:
            是否上传成功
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            logger.error(f"文件不存在: {abs_path}")
            return False

        # 重置事件
        self._chooser_event.clear()
        self._chooser_backend_node_id = None

        # 确保拦截已启用
        await self.cdp.send("Page.setInterceptFileChooserDialog", {"enabled": True})

        # 点击上传触发元素
        click_result = await self.cdp.send(
            "Runtime.evaluate",
            {"expression": click_js, "returnByValue": True},
        )
        clicked = click_result.get("result", {}).get("value", False)
        logger.info(f"上传触发点击结果: {clicked}")

        # 等待文件选择器打开
        try:
            await asyncio.wait_for(self._chooser_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("文件选择器未打开，尝试备选方案...")
            return await self._upload_via_dom_fallback(abs_path, click_js)

        # 用 DOM.setFileInputFiles 注入文件
        return await self._set_files_and_wait(abs_path, wait_upload_js)

    async def _set_files_and_wait(
        self,
        abs_path: str,
        wait_upload_js: Optional[str] = None,
    ) -> bool:
        """通过 backendNodeId 注入文件并等待上传完成。"""
        if self._chooser_backend_node_id is None:
            logger.error("未获取到文件选择器的 backendNodeId")
            return False

        try:
            await self.cdp.send(
                "DOM.setFileInputFiles",
                {
                    "files": [abs_path],
                    "backendNodeId": self._chooser_backend_node_id,
                },
            )
            logger.info(f"文件已注入: {abs_path}")
        except Exception as e:
            logger.error(f"文件注入失败: {e}")
            return False

        # 等待上传完成
        return await self._wait_upload_complete(wait_upload_js)

    async def _upload_via_dom_fallback(self, abs_path: str, click_js: str) -> bool:
        """
        备选方案：直接在 DOM 中查找 <input type="file"> 并设置文件。
        适用于文件选择器拦截不生效的情况。
        """
        logger.info("尝试 DOM 备选上传方案...")

        # 查找页面上所有 file input
        find_inputs_js = """
        (function() {
            var inputs = document.querySelectorAll('input[type="file"]');
            if (inputs.length === 0) return null;
            // 返回所有 input 的 backendNodeId
            return inputs.length;
        })()
        """
        result = await self.cdp.send(
            "Runtime.evaluate",
            {"expression": find_inputs_js, "returnByValue": True},
        )
        count = result.get("result", {}).get("value")
        if not count:
            logger.error("DOM 中未找到 <input type='file'> 元素")
            return False

        logger.info(f"找到 {count} 个 file input 元素")

        # 获取文档根节点
        doc = await self.cdp.send("DOM.getDocument", {"depth": 0})
        root_id = doc["root"]["nodeId"]

        # 查找 file input 的 nodeId
        query_result = await self.cdp.send(
            "DOM.querySelectorAll",
            {"nodeId": root_id, "selector": 'input[type="file"]'},
        )
        node_ids = query_result.get("nodeIds", [])
        if not node_ids:
            logger.error("querySelectorAll 未找到 file input")
            return False

        # 让所有 file input 可见并可用
        for nid in node_ids:
            await self.cdp.send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": """
                        function() {
                            this.style.display = 'block';
                            this.style.visibility = 'visible';
                            this.style.opacity = '1';
                            this.style.position = 'fixed';
                            this.style.top = '0';
                            this.style.left = '0';
                            this.style.zIndex = '999999';
                            this.removeAttribute('hidden');
                            this.removeAttribute('disabled');
                            this.accept = '*/*';
                        }
                    """,
                    "objectId": await self._node_id_to_object_id(nid),
                },
            )

        # 对第一个 file input 设置文件
        target_node_id = node_ids[0]
        try:
            await self.cdp.send(
                "DOM.setFileInputFiles",
                {"files": [abs_path], "nodeId": target_node_id},
            )
            logger.info(f"DOM 备选方案：文件已注入 (nodeId={target_node_id})")
            return await self._wait_upload_complete()
        except Exception as e:
            logger.error(f"DOM 备选方案文件注入失败: {e}")
            return False

    async def _node_id_to_object_id(self, node_id: int) -> str:
        """将 DOM nodeId 转换为 Runtime objectId。"""
        result = await self.cdp.send(
            "DOM.resolveNode",
            {"nodeId": node_id},
        )
        return result["object"]["objectId"]

    async def _wait_upload_complete(
        self,
        wait_js: Optional[str] = None,
        timeout: int = 60,
    ) -> bool:
        """等待上传完成。通过轮询 JS 或简单延时。"""
        if wait_js:
            # 使用自定义 JS 轮询上传状态
            start = time.time()
            while time.time() - start < timeout:
                result = await self.cdp.send(
                    "Runtime.evaluate",
                    {"expression": wait_js, "returnByValue": True},
                )
                done = result.get("result", {}).get("value", False)
                if done:
                    logger.info("上传完成（JS 确认）")
                    return True
                await asyncio.sleep(1)
            logger.warning(f"等待上传完成超时 ({timeout}s)")
            return False
        else:
            # 没有自定义 JS，等待一段时间让上传完成
            logger.info("等待上传完成（默认延时 5s）...")
            await asyncio.sleep(5)
            return True

    async def upload_via_paste(
        self,
        file_path: str,
        editor_selector: str = '.ProseMirror',
        timeout: int = 30,
        target_paragraph: int = None,
    ) -> dict:
        """
        通过剪贴板粘贴方式上传图片到头条 ProseMirror 编辑器（已验证有效）。

        核心原理：将图片数据转为 base64 → 注入页面构造 File 对象 →
        通过 ClipboardEvent('paste') 触发编辑器内置的粘贴上传处理器。

        Args:
            file_path:         要上传的本地文件绝对路径
            editor_selector:   编辑器 CSS 选择器
            timeout:           等待上传完成的最大秒数
            target_paragraph:  目标段落索引（0-based），图片将插入该段落之后。
                               为 None 时插入编辑器末尾。

        Returns:
            {"success": bool, "url": str, "width": int, "height": int}
        """
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            logger.error(f"文件不存在: {abs_path}")
            return {"success": False}

        import base64
        with open(abs_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("ascii")

        ext = Path(abs_path).suffix.lower().lstrip(".")
        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "gif": "image/gif", "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/png")
        filename = Path(abs_path).name

        # 记录上传前的图片数量
        before_count = await self._get_editor_image_count(editor_selector)

        # Step 1: 聚焦编辑器并定位光标
        if target_paragraph is not None:
            # 定位到指定段落末尾，实现图文穿插排版
            focus_js = """
            (function() {
                var editor = document.querySelector('%s');
                if (!editor) return {focused: false};
                editor.focus();
                var blocks = editor.querySelectorAll('p, h2, h3, blockquote');
                var target = blocks[%d];
                if (!target) return {focused: false, reason: 'paragraph not found'};
                var sel = window.getSelection();
                var range = document.createRange();
                range.selectNodeContents(target);
                range.collapse(false);
                sel.removeAllRanges();
                sel.addRange(range);
                return {focused: true, targetText: target.textContent.substring(0, 30)};
            })()
            """ % (editor_selector, target_paragraph)
        else:
            # 默认：光标移至编辑器末尾
            focus_js = """
            (function() {
                var editor = document.querySelector('%s');
                if (!editor) return {focused: false};
                editor.focus();
                var sel = window.getSelection();
                var range = document.createRange();
                range.selectNodeContents(editor);
                range.collapse(false);
                sel.removeAllRanges();
                sel.addRange(range);
                return {focused: true};
            })()
            """ % editor_selector
        focus_result = await self.cdp.send(
            "Runtime.evaluate",
            {"expression": focus_js, "returnByValue": True},
        )
        if not focus_result.get("result", {}).get("value", {}).get("focused"):
            logger.error("无法聚焦编辑器")
            return {"success": False}

        await asyncio.sleep(0.3)

        # Step 2: 构造并发送 ClipboardEvent
        paste_js = f"""
        (function() {{
            try {{
                var editor = document.querySelector('{editor_selector}');
                if (!editor) return {{success: false, reason: 'no editor'}};

                var b64 = '{img_b64}';
                var binaryStr = atob(b64);
                var bytes = new Uint8Array(binaryStr.length);
                for (var i = 0; i < binaryStr.length; i++) {{
                    bytes[i] = binaryStr.charCodeAt(i);
                }}
                var blob = new Blob([bytes], {{type: '{mime_type}'}});
                var file = new File([blob], '{filename}', {{type: '{mime_type}'}});

                var dt = new DataTransfer();
                dt.items.add(file);

                var pasteEvent = new ClipboardEvent('paste', {{
                    bubbles: true,
                    cancelable: true,
                    clipboardData: dt
                }});
                editor.dispatchEvent(pasteEvent);
                return {{success: true, fileSize: file.size}};
            }} catch(e) {{
                return {{success: false, error: e.message}};
            }}
        }})()
        """
        result = await self.cdp.send(
            "Runtime.evaluate",
            {"expression": paste_js, "returnByValue": True},
        )
        val = result.get("result", {}).get("value", {})
        if not val.get("success"):
            logger.error(f"粘贴事件发送失败: {json.dumps(val, ensure_ascii=False)}")
            return {"success": False}

        logger.info(f"粘贴事件已发送 ({filename}, {val.get('fileSize', 0)} bytes)")

        # Step 3: 轮询等待新图片出现在编辑器中
        uploaded_url = None
        start = time.time()
        while time.time() - start < timeout:
            await asyncio.sleep(2.5)
            check_js = """
            (function() {
                var editor = document.querySelector('%s');
                if (!editor) return {count: 0};
                var imgs = editor.querySelectorAll('img');
                return {
                    count: imgs.length,
                    lastImg: imgs.length > 0 ? {
                        src: imgs[imgs.length-1].src,
                        w: imgs[imgs.length-1].naturalWidth,
                        h: imgs[imgs.length-1].naturalHeight,
                        loaded: imgs[imgs.length-1].complete && imgs[imgs.length-1].naturalWidth > 0
                    } : null
                };
            })()
            """ % editor_selector
            check = await self.cdp.send(
                "Runtime.evaluate",
                {"expression": check_js, "returnByValue": True},
            )
            check_val = check.get("result", {}).get("value", {})
            current_count = check_val.get("count", 0)
            last_img = check_val.get("lastImg", {})

            if current_count > before_count and last_img and last_img.get("loaded"):
                uploaded_url = last_img.get("src", "")
                logger.info(
                    f"上传成功: {filename} -> {uploaded_url[:80]}... "
                    f"({last_img.get('w', 0)}x{last_img.get('h', 0)})"
                )
                return {
                    "success": True,
                    "url": uploaded_url,
                    "width": last_img.get("w", 0),
                    "height": last_img.get("h", 0),
                }

            if time.time() - start > 10 and time.time() - start < 12:
                logger.info(f"  等待上传中... ({current_count} 张图片)")

        logger.warning(f"上传超时 ({timeout}s): {filename}")
        return {"success": False}

    async def _get_editor_image_count(self, editor_selector: str = '.ProseMirror') -> int:
        """获取编辑器中当前图片数量。"""
        js = """
        (function() {
            var editor = document.querySelector('%s');
            return editor ? editor.querySelectorAll('img').length : 0;
        })()
        """ % editor_selector
        result = await self.cdp.send(
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True},
        )
        return result.get("result", {}).get("value", 0)


# ---------------------------------------------------------------------------
# 今日头条发布器
# ---------------------------------------------------------------------------
class ToutiaoPublisher:
    """
    今日头条图文发布器。

    通过 CDP 控制已登录的 Edge 浏览器，自动完成文章发布流程。

    使用示例:
        async with ToutiaoPublisher(port=9222) as pub:
            await pub.publish_article(
                title="我的文章",
                content="<p>正文内容</p>",
                image_paths=["cover.jpg"],
            )
    """

    # 今日头条创作平台 URL
    EDITOR_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
    MP_HOME_URL = "https://mp.toutiao.com"

    # ---- 可配置的选择器（头条编辑器可能更新，按需修改） ----
    SELECTORS = {
        # 标题输入框
        "title": [
            'textarea[placeholder*="标题"]',
            'input[placeholder*="标题"]',
            ".article-title textarea",
            ".article-title input",
            '[class*="title"] textarea',
            '[class*="title"] input',
        ],
        # 正文编辑器
        "editor": [
            ".ProseMirror",
            '[contenteditable="true"]',
            ".ql-editor",
            ".editor-content",
            ".bf-content",
            '[class*="editor"] [contenteditable="true"]',
        ],
        # 图片上传触发按钮
        "image_upload_btn": [
            '[class*="upload"] button',
            '[class*="image-upload"]',
            'button:has([class*="image"])',
            ".toolbar-btn-img",
            '[title*="图片"]',
            '[title*="插入图片"]',
        ],
        # 隐藏的 file input
        "file_input": [
            'input[type="file"]',
        ],
        # 发布按钮
        "publish_btn": [
            'button:has-text("发布")',
            'button:has-text("Publish")',
            'span:text("发布")',
            '[class*="publish"] button',
            ".publish-btn",
        ],
    }

    def __init__(self, host: str = "localhost", port: int = 9222):
        self.host = host
        self.port = port
        self.cdp = CDPClient()
        self.upload_handler: Optional[FileUploadHandler] = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    # -- 连接管理 --

    async def connect(self):
        """连接到浏览器并初始化。"""
        await self.cdp.connect(self.host, self.port)
        self.upload_handler = FileUploadHandler(self.cdp)
        await self.upload_handler.enable_interception()
        logger.info("发布器已连接并初始化")

    async def close(self):
        """关闭连接。"""
        await self.cdp.close()

    # -- JS 执行辅助 --

    async def eval_js(self, expression: str, await_promise: bool = False) -> any:
        """执行 JavaScript 并返回值。"""
        result = await self.cdp.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        return result.get("result", {}).get("value")

    async def wait_for_element(self, selector: str, timeout: int = 15) -> bool:
        """等待页面中出现指定选择器的元素。"""
        start = time.time()
        while time.time() - start < timeout:
            found = await self.eval_js(f"!!document.querySelector('{selector}')")
            if found:
                return True
            await asyncio.sleep(0.5)
        return False

    async def find_and_click(self, selectors: list[str], description: str = "元素") -> bool:
        """尝试多个选择器，找到第一个存在的元素并点击。"""
        for sel in selectors:
            try:
                clicked = await self.eval_js(f"""
                    (function() {{
                        var el = document.querySelector(`{sel}`);
                        if (el) {{ el.click(); return true; }}
                        return false;
                    }})()
                """)
                if clicked:
                    logger.info(f"已点击{description}: {sel}")
                    return True
            except Exception:
                continue
        logger.warning(f"未找到可点击的{description}")
        return False

    async def find_element_value(self, selectors: list[str]) -> Optional[str]:
        """尝试多个选择器，返回第一个找到的元素的值。"""
        for sel in selectors:
            try:
                val = await self.eval_js(f"""
                    (function() {{
                        var el = document.querySelector(`{sel}`);
                        return el ? (el.value || el.textContent || el.innerText) : null;
                    }})()
                """)
                if val:
                    return val
            except Exception:
                continue
        return None

    # -- 核心发布流程 --

    async def navigate_to_editor(self):
        """导航到文章编辑页面。"""
        current_url = await self.eval_js("window.location.href")
        if current_url and "graphic/publish" in current_url:
            logger.info("已在编辑页面")
            return

        logger.info(f"导航到编辑页面: {self.EDITOR_URL}")
        await self.cdp.send(
            "Page.navigate",
            {"url": self.EDITOR_URL},
        )
        await asyncio.sleep(5)  # 等待页面加载

        # 再等一会儿确认加载完成
        for _ in range(10):
            ready = await self.eval_js("document.readyState")
            if ready == "complete":
                break
            await asyncio.sleep(1)

        logger.info("编辑页面已加载")

    async def fill_title(self, title: str) -> bool:
        """
        填写文章标题。

        通过模拟用户输入的方式（包括 focus、输入值、触发事件），
        确保 React/Vue 等框架能正确捕获变更。
        """
        logger.info(f"填写标题: {title[:30]}...")

        js = f"""
        (function() {{
            var selectors = {json.dumps(self.SELECTORS['title'])};
            var title = {json.dumps(title)};

            for (var i = 0; i < selectors.length; i++) {{
                var el = document.querySelector(selectors[i]);
                if (el) {{
                    el.focus();
                    // 使用 native setter 确保 React 能捕获变更
                    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        el.tagName === 'TEXTAREA'
                            ? window.HTMLTextAreaElement.prototype
                            : window.HTMLInputElement.prototype,
                        'value'
                    );
                    if (nativeInputValueSetter && nativeInputValueSetter.set) {{
                        nativeInputValueSetter.set.call(el, title);
                    }} else {{
                        el.value = title;
                    }}
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                    return true;
                }}
            }}
            return false;
        }})()
        """
        result = await self.eval_js(js)
        if result:
            logger.info("标题填写成功")
        else:
            logger.error("未找到标题输入框，请检查 SELECTORS['title'] 配置")
        return bool(result)

    async def insert_content(self, content: str, is_html: bool = True) -> bool:
        """
        插入文章正文。

        Args:
            content: 正文内容（HTML 或纯文本）
            is_html: 如果为 True，将 content 当作 HTML 插入
        """
        logger.info("插入文章正文...")

        # 将正文中的本地图片路径替换为占位符（后续上传后替换）
        # content 中的 <img src="local_path"> 会在上传后被替换

        if not is_html:
            # 纯文本转 HTML
            paragraphs = content.split("\n\n")
            content = "".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paragraphs)

        js = f"""
        (function() {{
            var selectors = {json.dumps(self.SELECTORS['editor'])};
            var htmlContent = {json.dumps(content)};

            for (var i = 0; i < selectors.length; i++) {{
                var editor = document.querySelector(selectors[i]);
                if (editor && editor.getAttribute('contenteditable') === 'true') {{
                    editor.focus();

                    // 尝试使用 execCommand（兼容大多数编辑器）
                    document.execCommand('selectAll', false, null);
                    var inserted = document.execCommand('insertHTML', false, htmlContent);

                    if (!inserted) {{
                        // 回退：直接设置 innerHTML
                        editor.innerHTML = htmlContent;
                    }}

                    // 触发事件让编辑器框架感知变更
                    editor.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    editor.dispatchEvent(new Event('change', {{ bubbles: true }}));

                    // 对 ProseMirror 编辑器额外触发
                    if (editor.classList.contains('ProseMirror')) {{
                        var tr = new Event('compositionend', {{ bubbles: true }});
                        editor.dispatchEvent(tr);
                    }}

                    return true;
                }}
            }}
            return false;
        }})()
        """
        result = await self.eval_js(js)
        if result:
            logger.info("正文插入成功")
        else:
            logger.error("未找到正文编辑器，请检查 SELECTORS['editor'] 配置")
        return bool(result)

    async def upload_cover_image(self, image_path: str) -> bool:
        """
        上传文章封面图。

        采用三级上传策略：
          1. CDP 文件选择器拦截 + DOM.setFileInputFiles（推荐）
          2. DOM 直接操作 <input type="file">（备选）
          3. 剪贴板粘贴（最终备选）
        """
        abs_path = os.path.abspath(image_path)
        if not os.path.isfile(abs_path):
            logger.error(f"封面图片不存在: {abs_path}")
            return False

        logger.info(f"上传封面图: {abs_path}")

        # --- 策略1: 点击上传按钮触发文件选择器 ---
        # 先查找并点击上传按钮
        click_js = f"""
        (function() {{
            var selectors = {json.dumps(self.SELECTORS['image_upload_btn'])};
            for (var i = 0; i < selectors.length; i++) {{
                var btn = document.querySelector(selectors[i]);
                if (btn) {{
                    btn.click();
                    return true;
                }}
            }}
            // 也尝试直接点击 file input 的父容器
            var fileInput = document.querySelector('input[type="file"]');
            if (fileInput && fileInput.parentElement) {{
                fileInput.parentElement.click();
                return true;
            }}
            return false;
        }})()
        """

        success = await self.upload_handler.upload_via_click(
            file_path=abs_path,
            click_js=click_js,
            timeout=10,
        )

        if success:
            logger.info("封面图上传成功（策略1）")
            return True

        # --- 策略2: 直接操作 DOM file input ---
        logger.info("策略1失败，尝试直接操作 file input...")
        make_visible_js = """
        (function() {
            var inputs = document.querySelectorAll('input[type="file"]');
            for (var i = 0; i < inputs.length; i++) {
                inputs[i].style.display = 'block';
                inputs[i].style.visibility = 'visible';
                inputs[i].style.opacity = '1';
                inputs[i].style.position = 'fixed';
                inputs[i].style.top = '0';
                inputs[i].style.left = '0';
                inputs[i].style.zIndex = '999999';
                inputs[i].removeAttribute('hidden');
                inputs[i].removeAttribute('disabled');
                inputs[i].accept = 'image/*';
            }
            return inputs.length;
        })()
        """
        count = await self.eval_js(make_visible_js)
        if count and count > 0:
            # 获取 file input 的 nodeId 并设置文件
            doc = await self.cdp.send("DOM.getDocument", {"depth": 0})
            root_id = doc["root"]["nodeId"]
            query = await self.cdp.send(
                "DOM.querySelectorAll",
                {"nodeId": root_id, "selector": 'input[type="file"]'},
            )
            node_ids = query.get("nodeIds", [])
            if node_ids:
                try:
                    await self.cdp.send(
                        "DOM.setFileInputFiles",
                        {"files": [abs_path], "nodeId": node_ids[0]},
                    )
                    logger.info("封面图上传成功（策略2）")
                    await asyncio.sleep(5)
                    return True
                except Exception as e:
                    logger.warning(f"策略2失败: {e}")

        # --- 策略3: 剪贴板粘贴 ---
        logger.info("策略2失败，尝试粘贴上传...")
        success = await self.upload_handler.upload_via_paste(abs_path)
        if success:
            logger.info("封面图上传成功（策略3）")
        else:
            logger.error("所有封面图上传策略均失败")
        return success

    async def upload_content_images(self, image_paths: list[str]) -> list[dict]:
        """
        上传文章正文中的图片（通过剪贴板粘贴方式）。

        每张图片会按照段落数量均匀分布插入，实现图文穿插排版：
          - N 张图，M 个段落 → 第 i 张图插入到第 round((i+1) * M / (N+1)) 段之后
          - 例如 4 张图 10 段 → 插入到第 2、4、6、8 段之后

        每个结果包含 {"success": bool, "url": str, "path": str}
        """
        if not image_paths:
            return []

        # 计算段落数，确定每张图片的目标位置
        para_count = await self.eval_js("""
            (function() {
                var editor = document.querySelector('.ProseMirror');
                if (!editor) return 0;
                var blocks = editor.querySelectorAll('p, h2, h3, blockquote');
                return blocks.length;
            })()
        """)
        if not para_count or para_count <= 0:
            logger.warning("无法获取编辑器段落数，回退到末尾粘贴")
            para_count = 0

        n_images = len(image_paths)
        n_paras = para_count

        uploaded = []
        for idx, img_path in enumerate(image_paths):
            abs_path = os.path.abspath(img_path)
            if not os.path.isfile(abs_path):
                logger.warning(f"跳过不存在的图片: {abs_path}")
                continue

            # 计算当前图片应插入的段落索引
            if n_paras > 0 and n_images > 1:
                target_para = round((idx + 1) * n_paras / (n_images + 1)) - 1
                target_para = max(0, min(target_para, n_paras - 1))
            else:
                target_para = None

            logger.info(
                f"上传正文图片 [{idx+1}/{n_images}]: {Path(abs_path).name}"
                + (f" → 第{target_para+1}段之后" if target_para is not None else " → 末尾")
            )

            result = await self.upload_handler.upload_via_paste(
                abs_path, target_paragraph=target_para
            )
            result["path"] = abs_path

            if result.get("success"):
                uploaded.append(result)
                logger.info(f"  OK [{idx+1}/{n_images}] 上传成功")
            else:
                logger.error(f"  FAIL [{idx+1}/{n_images}] 上传失败")

            # 图片间稍作间隔
            if idx < n_images - 1:
                await asyncio.sleep(1)

        logger.info(f"正文图片上传完成: {len(uploaded)}/{n_images} 成功")

        # 上传完成后扫描编辑器，检测图片异常状态
        editor_errors = await self._check_editor_for_image_errors()
        if editor_errors:
            logger.warning(
                f"编辑器图片异常: {len(editor_errors)} 处，建议关注"
                f"（{len(uploaded)}/{n_images} 上传成功）"
            )

        return uploaded

    async def click_publish(self, title: Optional[str] = None, mark_ai_generated: bool = False) -> bool:
        """
        点击发布按钮（头条两步流程：预览并发布 → 勾选引用AI → 确认发布）。

        头条创作平台的发布流程：
          1. 点击底部的"预览并发布"按钮
          2. 页面显示预览确认区域，出现"引用AI"等声明选项和"确认发布"/"返回编辑"按钮
          3. 如果 mark_ai_generated=True，勾选"引用AI"复选框
          4. 启用 Network 拦截，监听 https://mp.toutiao.com/mp/agw/article/publish 响应
          5. 点击"确认发布"，从接口响应中直接提取 pgc_id（最可靠）
          6. 若网络拦截超时（5s），回退到页面跳转检测 + 标题匹配 / URL-DOM / CDP target 提取

        Args:
            title:              文章标题。传入后，当其他方式提取 pgc_id 失败时，
                                会自动在管理列表中按标题匹配提取 pgc_id。
            mark_ai_generated:  是否勾选"引用AI"作品声明。图片带 AI 水印时设为 True。

        Returns:
            发布结果字典 {"success": bool, "pgc_id": str|None}
        """
        logger.info("准备发布文章...")

        # 先滚动到底部确保发布按钮可见
        await self.eval_js("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)

        # 关闭可能遮挡发布按钮的弹窗（写作助手、AI 侧栏等）
        await self.eval_js("""
            (function() {
                var close_btns = document.querySelectorAll(
                    '[class*="close"], [class*="Close"], [class*="drawer"] [class*="close"], '
                    + '.byte-modal-close, .ai-assistant-close, .sidebar-close, '
                    + '[aria-label*="关闭"], [aria-label*="close"], '
                    + '.popup-close, .dialog-close, .overlay-close'
                );
                for (var i = 0; i < close_btns.length; i++) {
                    var el = close_btns[i];
                    var rect = el.getBoundingClientRect();
                    if (rect.height > 0 && rect.width > 0) {
                        el.click();
                        return {closed: true};
                    }
                }
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
                return {closed: false, action: 'ESC'};
            })()
        """)
        await asyncio.sleep(1)

        # === 第一步：点击"预览并发布" ===
        step1_js = """
        (function() {
            var buttons = document.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var text = buttons[i].textContent.trim();
                var rect = buttons[i].getBoundingClientRect();
                if (rect.height > 0 && rect.width > 0 &&
                    (text === '预览并发布' || text === '发布')) {
                    buttons[i].click();
                    return {clicked: true, text: text};
                }
            }
            return {clicked: false};
        })()
        """
        step1 = await self.eval_js(step1_js)
        logger.info(f"第一步: {json.dumps(step1, ensure_ascii=False)}")

        if not step1 or not step1.get("clicked"):
            logger.error("未找到发布按钮")
            return {"success": False, "pgc_id": None}

        # 等待确认区域出现（轮询等待，最多 15 秒）
        logger.info("等待确认发布按钮出现...")
        confirm_btn_found = False
        for attempt in range(30):
            await asyncio.sleep(0.5)
            # 先检查 URL 是否已直接跳转（有些文章直接发布）
            url = await self.eval_js("window.location.href")
            if url and "articles" in url and "publish" not in url:
                logger.info(f"发布成功！URL: {url}")
                pgc_id = await self._extract_pgc_id()
                if pgc_id:
                    logger.info(f"文章 ID: {pgc_id}")
                return {"success": True, "pgc_id": pgc_id}
            # 检查确认发布按钮是否已经渲染
            probe = await self.eval_js("""
                (function() {
                    var buttons = document.querySelectorAll('button');
                    for (var i = 0; i < buttons.length; i++) {
                        var text = buttons[i].textContent.trim();
                        var rect = buttons[i].getBoundingClientRect();
                        if (text === '确认发布' && rect.height > 0 && rect.width > 0) {
                            return {found: true};
                        }
                    }
                    return {found: false};
                })()
            """)
            if probe and probe.get("found"):
                confirm_btn_found = True
                logger.info(f"确认发布按钮已就绪（第 {attempt+1} 次检测）")
                break

        if not confirm_btn_found:
            logger.warning("等待超时，仍尝试查找确认发布按钮")
            # 额外等 2 秒后最后尝试一次
            await asyncio.sleep(2)

        # === 第二步：勾选"引用AI"（如需要） ===
        if mark_ai_generated:
            logger.info("勾选「引用AI」声明...")
            ai_check_js = """
            (function() {
                var spans = document.querySelectorAll('.byte-checkbox-inner-text');
                for (var i = 0; i < spans.length; i++) {
                    if (spans[i].textContent.trim() === '引用AI') {
                        spans[i].parentElement.click();
                        return {clicked: true};
                    }
                }
                return {clicked: false};
            })()
            """
            ai_result = await self.eval_js(ai_check_js)
            logger.info(f"引用AI勾选: {json.dumps(ai_result, ensure_ascii=False)}")
            if ai_result and ai_result.get("clicked"):
                await asyncio.sleep(0.5)

        # === 第三步：JS 猴子补丁拦截发布接口响应 ===
        # 不走 CDP Network 域（依赖 WebSocket 事件时序，页面导航时可能丢失）。
        # 直接在页面 JS 上下文 monkey-patch XHR / fetch，同步捕获 article/publish 响应。
        logger.info("注入 JS 拦截器，监听发布接口...")
        patch_js = """
        (function() {
            if (window.__marvis_pgc_id) return;
            window.__marvis_publish_response = null;
            // --- XMLHttpRequest monkey-patch ---
            var _origXHROpen = XMLHttpRequest.prototype.open;
            var _origXHRSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.open = function(method, url) {
                this.__mv_url = url;
                return _origXHROpen.apply(this, arguments);
            };
            XMLHttpRequest.prototype.send = function(body) {
                var xhr = this;
                if (xhr.__mv_url && xhr.__mv_url.indexOf('/article/publish') !== -1) {
                    xhr.addEventListener('readystatechange', function() {
                        if (xhr.readyState === 4 && xhr.status === 200) {
                            try {
                                var resp = JSON.parse(xhr.responseText);
                                window.__marvis_publish_response = resp;
                                if (resp.data && resp.data.pgc_id) {
                                    window.__marvis_pgc_id = resp.data.pgc_id;
                                }
                            } catch(e) {}
                        }
                    });
                }
                return _origXHRSend.apply(this, arguments);
            };
            // --- fetch monkey-patch ---
            var _origFetch = window.fetch;
            window.fetch = function(url, options) {
                return _origFetch.apply(this, arguments).then(function(response) {
                    var urlStr = (typeof url === 'string') ? url : (url && url.url ? url.url : '');
                    if (urlStr.indexOf('/article/publish') !== -1) {
                        response.clone().json().then(function(data) {
                            window.__marvis_publish_response = data;
                            if (data.data && data.data.pgc_id) {
                                window.__marvis_pgc_id = data.data.pgc_id;
                            }
                        }).catch(function(){});
                    }
                    return response;
                });
            };
        })()
        """
        await self.eval_js(patch_js)

        # === 第四步：点击"确认发布" ===
        step2_js = """
        (function() {
            var buttons = document.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var text = buttons[i].textContent.trim();
                var rect = buttons[i].getBoundingClientRect();
                if (text === '确认发布' && rect.height > 0 && rect.width > 0) {
                    buttons[i].click();
                    return {clicked: true, text: text};
                }
            }
            return {clicked: false};
        })()
        """
        step2 = await self.eval_js(step2_js)
        logger.info(f"第三步: {json.dumps(step2, ensure_ascii=False)}")

        if not step2 or not step2.get("clicked"):
            logger.error("未找到确认发布按钮")
            return {"success": False, "pgc_id": None}

        # 轮询 JS 拦截器存储的 pgc_id（点击后页面导航前捕获）
        # 200ms 间隔 × 25 次 = 5s，窗口期内页面未导航即可拿到
        for poll_i in range(25):
            await asyncio.sleep(0.2)
            try:
                raw = await self.eval_js("window.__marvis_pgc_id || ''")
                if raw and str(raw).strip() and str(raw).strip() != 'undefined':
                    pgc_id = str(raw).strip().strip('"').strip("'")
                    if pgc_id and pgc_id.isdigit() and len(pgc_id) >= 16:
                        logger.info(f"JS 拦截器捕获 pgc_id: {pgc_id}")
                        # 同时检查发布响应中的图片异常信息
                        image_warnings = await self._get_publish_response_image_warnings()
                        return {"success": True, "pgc_id": pgc_id, "image_warnings": image_warnings}
            except Exception:
                pass  # CDP 可能已断连，退出轮询走回退
            if poll_i > 3:
                # 第 4 次轮询起检查是否已跳转
                try:
                    url = await self.eval_js("window.location.href")
                    if url and ("articles" in url and "publish" not in url):
                        break
                except Exception:
                    break

        logger.info("JS 拦截器未捕获 pgc_id，回退到页面跳转检测...")

        # 回退：页面跳转检测
        await asyncio.sleep(1)
        for i in range(10):
            await asyncio.sleep(2)
            url = await self.eval_js("window.location.href")
            if url and "articles" in url and "publish" not in url:
                logger.info(f"发布成功！URL: {url}")
                pgc_id = None
                if title:
                    pgc_id = await self._find_pgc_by_title(title)
                if not pgc_id:
                    pgc_id = await self._extract_pgc_id()
                if not pgc_id:
                    pgc_id = await self._find_pgc_id_in_targets()
                if pgc_id:
                    logger.info(f"文章 ID: {pgc_id}")
                return {"success": True, "pgc_id": pgc_id}
            success = await self.eval_js("""
                (function() {
                    var els = document.querySelectorAll('[class*="success"], [class*="toast"], [class*="Toast"]');
                    for (var i = 0; i < els.length; i++) {
                        var rect = els[i].getBoundingClientRect();
                        if (rect.height > 0) return els[i].textContent.trim().substring(0, 50);
                    }
                    return '';
                })()
            """)
            if success:
                logger.info(f"成功提示: {success}")
                pgc_id = await self._find_pgc_id_in_targets()
                if not pgc_id and title:
                    pgc_id = await self._find_pgc_by_title(title)
                return {"success": True, "pgc_id": pgc_id}

        pgc_id = await self._find_pgc_id_in_targets()
        if not pgc_id and title:
            pgc_id = await self._find_pgc_by_title(title)
        if pgc_id:
            logger.info(f"获取到文章 ID: {pgc_id}")
            return {"success": True, "pgc_id": pgc_id}

        logger.warning("发布后未检测到页面跳转，请手动确认")
        return {"success": False, "pgc_id": None}

    async def _get_publish_response_image_warnings(self) -> list[str]:
        """
        从 JS 拦截器捕获的发布接口响应中提取图片异常信息。

        从 window.__marvis_publish_response 中提取与图片相关的警告/错误信息，
        常见字段：message 含"图片链接异常"、data.image_errors 等。
        """
        try:
            resp_json = await self.eval_js(
                "JSON.stringify(window.__marvis_publish_response || '')"
            )
            if not resp_json or resp_json == '""' or resp_json == "''":
                return []
        except Exception:
            return []

        warnings = []
        try:
            resp = json.loads(resp_json)
        except (json.JSONDecodeError, TypeError):
            return []

        # 检查顶层 message 是否包含图片异常关键词
        msg = str(resp.get("message", ""))
        if "图片" in msg or "image" in msg.lower():
            warnings.append(f"message: {msg}")

        data = resp.get("data", {}) if isinstance(resp, dict) else {}
        # 递归扫描 data 中的图片相关字段
        def _scan(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    _p = f"{path}.{k}" if path else k
                    if isinstance(v, str) and ("图片" in v or "image" in v.lower()):
                        warnings.append(f"{_p}: {str(v)[:200]}")
                    elif isinstance(v, (dict, list)):
                        _scan(v, _p)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    _scan(v, f"{path}[{i}]")

        _scan(data, "data")
        logger.info(f"发布响应图片异常检测: {len(warnings)} 条警告")
        for w in warnings:
            logger.warning(f"  {w}")
        return warnings

    async def _check_editor_for_image_errors(self) -> list[dict]:
        """
        扫描编辑器 DOM 中是否存在图片错误状态。

        检查：img 元素的 src 是否为 blob: 或 data:、是否包含 error class、
              是否有"图片链接异常"文本节点等。
        """
        check_js = """
        (function() {
            var results = [];
            var editor = document.querySelector('.ProseMirror');
            if (!editor) return results;

            // 1. 检查所有 img 元素
            var imgs = editor.querySelectorAll('img');
            for (var i = 0; i < imgs.length; i++) {
                var img = imgs[i];
                var issues = [];

                // blob: / data: URL 表示图片尚未被平台服务端接收
                if (img.src && (img.src.startsWith('blob:') || img.src.startsWith('data:'))) {
                    issues.push('local_url:' + img.src.substring(0, 50));
                }

                // naturalWidth=0 且 complete=true 表示图片加载失败
                if (img.complete && img.naturalWidth === 0 && img.src && !img.src.startsWith('data:')) {
                    issues.push('broken_image');
                }

                // class 含 error/fail 关键词
                var cls = img.className || '';
                var parentCls = (img.parentElement && img.parentElement.className) || '';
                var grandCls = (img.parentElement && img.parentElement.parentElement && img.parentElement.parentElement.className) || '';
                if (/error|fail|broken/i.test(cls + ' ' + parentCls + ' ' + grandCls)) {
                    issues.push('error_class');
                }

                if (issues.length > 0) {
                    results.push({
                        index: i,
                        src: img.src ? img.src.substring(0, 80) : '',
                        issues: issues
                    });
                }
            }

            // 2. 搜索文本节点中的错误提示
            var walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT, null, false);
            var textNode;
            while (textNode = walker.nextNode()) {
                var txt = textNode.textContent.trim();
                if (txt.indexOf('图片链接异常') !== -1 || txt.indexOf('上传失败') !== -1) {
                    results.push({index: -1, src: '', issues: ['error_text:' + txt.substring(0, 80)]});
                    break;
                }
            }

            return results;
        })()
        """
        try:
            errors = await self.eval_js(check_js)
            if errors and len(errors) > 0:
                logger.warning(f"编辑器图片异常检测: {len(errors)} 条")
                for e in errors:
                    logger.warning(f"  img[{e.get('index')}] {e.get('issues')} src={e.get('src', '')[:60]}")
            return errors or []
        except Exception as e:
            logger.warning(f"编辑器图片异常检测失败: {e}")
            return []

    async def _extract_pgc_id(self) -> Optional[str]:
        """
        从当前页面提取文章 pgc_id。

        尝试顺序：
          1. URL 路径或查询参数 (如 /content/7650390125007962667 或 ?pgc_id=7650451529836675638)
          2. 页面中暴露的数据属性或链接
          3. 文章管理列表中最新文章链接（发布后跳转到管理页时使用）
        """
        # 方式1: URL 路径匹配 + query 参数匹配
        url = await self.eval_js("window.location.href")
        if url:
            # 1a: query 参数 ?pgc_id=xxx 或 &pgc_id=xxx
            m = re.search(r'[?&]pgc_id=(\d{16,20})', url)
            if m:
                return m.group(1)
            # 1b: 路径中的数字 ID
            m = re.search(r'/(\d{16,20})', url)
            if m:
                return m.group(1)

        # 方式2: 页面中的 data-pgc-id 或包含 pgc_id 的链接
        pgc_id = await self.eval_js("""
            (function() {
                // data 属性
                var el = document.querySelector('[data-pgc-id], [data-article-id]');
                if (el) return el.getAttribute('data-pgc-id') || el.getAttribute('data-article-id');
                return '';
            })()
        """)
        if pgc_id:
            return pgc_id

        # 方式3: 文章管理列表页 - 从所有链接中提取最新文章的 pgc_id
        # 发布成功后跳转到管理列表页 (/profile_v4/manage/content)，URL 不含 pgc_id，
        # 但页面 DOM 中的文章链接包含 item/数字 或 content/数字 格式的 ID
        pgc_id = await self.eval_js("""
            (function() {
                // 优先查找管理列表中的文章链接（头条管理页格式：/item/数字）
                var allLinks = document.querySelectorAll('a[href*="/item/"]');
                var ids = [];
                for (var i = 0; i < allLinks.length; i++) {
                    var m = allLinks[i].href.match(/\\/(\\d{16,20})/);
                    if (m) ids.push(m[1]);
                }
                // 返回第一个（通常是列表中最新的）
                if (ids.length > 0) return ids[0];

                // 备选：content 格式链接
                var contentLinks = document.querySelectorAll('a[href*="/content/"]');
                for (var j = 0; j < contentLinks.length; j++) {
                    var m2 = contentLinks[j].href.match(/\\/(\\d{16,20})/);
                    if (m2) return m2[1];
                }
                return '';
            })()
        """)
        if pgc_id:
            return pgc_id

        return None

    async def _find_pgc_id_in_targets(self) -> Optional[str]:
        """
        枚举所有 CDP target，查找包含 pgc_id 的页面 URL。

        发布成功后可能打开新标签页（如预览页 ?pgc_id=xxx）或跳转到文章管理页，
        而当前 CDP 连接可能仍在旧 target 上。此方法遍历所有 page 类型 target，
        从 URL 中提取 pgc_id。
        """
        import urllib.request
        try:
            url = f"http://{self.host}:{self.port}/json"
            with urllib.request.urlopen(url, timeout=5) as resp:
                targets = json.loads(resp.read().decode())
        except Exception as e:
            logger.warning(f"无法枚举 CDP targets: {e}")
            return None

        for t in targets:
            if t.get("type") != "page":
                continue
            target_url = t.get("url", "")
            if not target_url:
                continue

            # 检查 URL 中是否包含 pgc_id
            m = re.search(r'[?&]pgc_id=(\d{16,20})', target_url)
            if m:
                logger.info(f"在 CDP target 中找到 pgc_id: {m.group(1)} (URL: {target_url[:80]})")
                return m.group(1)
            m = re.search(r'/(\d{16,20})', target_url)
            if m:
                logger.info(f"在 CDP target 中找到 pgc_id: {m.group(1)} (URL: {target_url[:80]})")
                return m.group(1)

        return None

    async def _find_pgc_by_title(self, title: str) -> Optional[str]:
        """
        在文章管理列表中按标题匹配，提取 pgc_id。
        最多重试 3 次，每次失败后刷新页面重新等待 SPA 列表渲染。

        发布成功后页面跳转到管理列表页，此方法在列表 DOM 中搜索
        标题文本匹配的文章行，从中提取 /item/数字 或 /content/数字 格式的链接。
        支持模糊匹配（标题前 15 个字符包含在页面标题中即算匹配）。

        Returns:
            pgc_id 字符串，或 None
        """
        keyword = title.strip()[:15]
        logger.info(f"在管理列表中匹配标题: {title[:30]}... (keyword={keyword})")

        async def _search():
            return await self.eval_js(f"""
                (function() {{
                    var keyword = {json.dumps(keyword)};
                    // 策略1: 直接匹配 <a> 标签自身
                    var allLinks = document.querySelectorAll('a[href*="/item/"], a[href*="/content/"]');
                    for (var i = 0; i < allLinks.length; i++) {{
                        var text = (allLinks[i].textContent || '').trim();
                        if (text.indexOf(keyword) >= 0) {{
                            var m = allLinks[i].href.match(/\\/(\\d{{16,20}})/);
                            if (m) return m[1];
                        }}
                    }}
                    // 策略2: 在列表行中查找
                    var rows = document.querySelectorAll('tr, [class*="row"], [class*="item"], li');
                    for (var k = 0; k < rows.length; k++) {{
                        var rowText = (rows[k].textContent || '').trim();
                        if (rowText.indexOf(keyword) >= 0) {{
                            var rowLinks = rows[k].querySelectorAll('a[href*="/item/"], a[href*="/content/"]');
                            for (var l = 0; l < rowLinks.length; l++) {{
                                var m2 = rowLinks[l].href.match(/\\/(\\d{{16,20}})/);
                                if (m2) return m2[1];
                            }}
                        }}
                    }}
                    return '';
                }})()
            """)

        async def _reload_and_wait():
            await self.cdp.send("Page.reload")
            for _ in range(15):
                await asyncio.sleep(1)
                if await self.eval_js("document.readyState") == "complete":
                    break
            # 等待 SPA API 请求完成并渲染列表
            await asyncio.sleep(3)

        async def _navigate_and_wait():
            await self.cdp.send(
                "Page.navigate",
                {"url": "https://mp.toutiao.com/profile_v4/graphic/articles"},
            )
            for _ in range(15):
                await asyncio.sleep(1)
                ready = await self.eval_js("document.readyState")
                link_count = await self.eval_js("document.querySelectorAll('a').length")
                if ready == "complete" and link_count > 20:
                    break
            logger.info(f"管理列表页已加载 (links={link_count})")
            await asyncio.sleep(3)

        for attempt in range(3):
            if attempt == 0:
                # 首次：在管理页则刷新复用，否则导航
                current_url = await self.eval_js("window.location.href") or ""
                if "graphic/articles" not in current_url:
                    logger.info("导航到文章管理页...")
                    await _navigate_and_wait()
                else:
                    logger.info("已在管理页，刷新列表...")
                    await _reload_and_wait()
            else:
                logger.info(f"第 {attempt+1} 次重试：刷新页面等待列表渲染...")
                await _reload_and_wait()

            pgc_id = await _search()
            if pgc_id:
                logger.info(f"标题匹配成功: {title[:30]}... → pgc_id={pgc_id}")
                return pgc_id
            logger.warning(f"标题匹配失败（第 {attempt+1}/3 次）: {title[:30]}...")

        logger.error(f"3 次重试后标题匹配仍失败: {title[:30]}...")
        return None

    async def select_cover_mode(self, mode: str = "三图") -> bool:
        """
        选择封面模式（单图/三图/无封面）。

        头条编辑器在粘贴正文图片后会自动识别并填充封面。
        此方法用于确保封面模式设置正确。

        Args:
            mode: "单图"、"三图" 或 "无封面"
        """
        logger.info(f"设置封面模式: {mode}")
        js = f"""
        (function() {{
            var radios = document.querySelectorAll('.byte-radio');
            for (var i = 0; i < radios.length; i++) {{
                var text = radios[i].textContent.trim();
                if (text === '{mode}') {{
                    radios[i].click();
                    return {{clicked: true, text: text}};
                }}
            }}
            // 备选：通过 label 文字查找
            var labels = document.querySelectorAll('label');
            for (var i = 0; i < labels.length; i++) {{
                if (labels[i].textContent.trim() === '{mode}') {{
                    labels[i].click();
                    return {{clicked: true, text: '{mode}', method: 'label'}};
                }}
            }}
            return {{clicked: false}};
        }})()
        """
        result = await self.eval_js(js)
        logger.info(f"封面模式: {json.dumps(result, ensure_ascii=False)}")
        return bool(result and result.get("clicked"))

    async def get_editor_images(self) -> list[str]:
        """获取编辑器正文中所有已上传图片的 URL。"""
        js = """
        (function() {
            var editor = document.querySelector('.ProseMirror');
            if (!editor) return [];
            var imgs = editor.querySelectorAll('img');
            return Array.from(imgs).map(function(img) {
                return img.src;
            });
        })()
        """
        return await self.eval_js(js) or []

    # -- 完整发布流程 --

    async def publish_article(
        self,
        title: str,
        content: str,
        image_paths: Optional[list[str]] = None,
        content_image_paths: Optional[list[str]] = None,
        is_html: bool = True,
        auto_publish: bool = False,
        cover_mode: str = "三图",
        dry_run: bool = False,
        mark_ai_generated: bool = False,
        article_dir: Optional[str] = None,
    ) -> dict:
        """
        完整的文章发布流程（全自动，零手动步骤）。

        已验证流程：
          1. 导航到编辑页
          2. 填写标题
          3. 插入正文 HTML
          4. 通过剪贴板粘贴上传图片（自动上传到头条服务器）
          5. 设置封面模式（编辑器自动从正文图片选取封面）
          6. 点击"预览并发布" → 勾选引用AI → "确认发布"

        Args:
            title:              文章标题
            content:            文章正文（HTML 或纯文本）
            image_paths:        封面图片路径列表（已废弃，封面从正文图片自动选取）
            content_image_paths: 正文中需要上传的图片路径列表
            is_html:            content 是否为 HTML 格式
            auto_publish:       是否自动点击发布
            cover_mode:         封面模式 ("单图"/"三图"/"无封面")
            dry_run:            试运行模式（只填写内容不发布）
            mark_ai_generated:  是否勾选"引用AI"作品声明
            article_dir:        文章目录路径（如 articles_published/2026/06/13/article1/）。
                                传入后，发布成功会自动写入文章链接文件并更新日期汇总。

        Returns:
            包含各步骤结果的字典
        """
        result = {
            "title_ok": False,
            "content_ok": False,
            "images_uploaded": [],
            "cover_mode_ok": False,
            "published": False,
        }

        # Step 1: 导航到编辑页
        await self.navigate_to_editor()
        await asyncio.sleep(2)

        # Step 2: 填写标题
        result["title_ok"] = await self.fill_title(title)
        await asyncio.sleep(1)

        # Step 3: 插入正文
        result["content_ok"] = await self.insert_content(content, is_html)
        await asyncio.sleep(1)

        # Step 4: 上传正文图片（剪贴板粘贴方式）
        if content_image_paths:
            result["images_uploaded"] = await self.upload_content_images(
                content_image_paths
            )
        
        # Step 5: 设置封面模式（编辑器会自动从正文图片选取封面）
        if result["images_uploaded"]:
            result["cover_mode_ok"] = await self.select_cover_mode(cover_mode)
            await asyncio.sleep(2)

        # Step 6: 发布
        if auto_publish and not dry_run:
            pub_result = await self.click_publish(title=title, mark_ai_generated=mark_ai_generated)
            result["published"] = pub_result["success"]
            if pub_result.get("pgc_id"):
                result["pgc_id"] = pub_result["pgc_id"]
                # 自动写入文章链接文件 + 更新日期汇总
                if article_dir:
                    await self._save_article_link(
                        title=title,
                        pgc_id=pub_result["pgc_id"],
                        article_dir=article_dir,
                    )
        elif dry_run:
            logger.info("试运行模式：跳过发布")
        else:
            logger.info("文章已准备就绪，请在浏览器中手动确认并发布")

        # 汇总
        img_count = len(result["images_uploaded"])
        logger.info("=" * 50)
        logger.info("发布流程完成:")
        logger.info(f"  标题: {'OK' if result['title_ok'] else 'FAIL'}")
        logger.info(f"  正文: {'OK' if result['content_ok'] else 'FAIL'}")
        logger.info(f"  图片: {img_count} 张上传成功")
        logger.info(f"  封面: {'OK' if result['cover_mode_ok'] else '(未设置)'}")
        logger.info(f"  发布: {'OK' if result['published'] else '(未执行)'}")
        if result.get("pgc_id"):
            logger.info(f"  文章ID: {result['pgc_id']}")
        logger.info("=" * 50)

        return result

    # -- 链接持久化 --

    async def _save_article_link(
        self,
        title: str,
        pgc_id: str,
        article_dir: str,
    ):
        """
        发布成功后自动写入两处链接文件：

        1. 文章级：{article_dir}/文章链接.txt
           内容：https://www.toutiao.com/item/{pgc_id}/
                 pgc_id: {pgc_id}

        2. 日期汇总：articles_published/{year}/{month}/{day}/文章链接.txt
           格式：编号. [标题](链接)
           从 article_dir 路径中解析 year/month/day/article{N} 结构。
        """
        import re

        article_dir = os.path.normpath(article_dir)
        article_link_url = f"https://www.toutiao.com/item/{pgc_id}/"

        # --- 写入文章级链接 ---
        article_link_path = os.path.join(article_dir, "文章链接.txt")
        os.makedirs(article_dir, exist_ok=True)
        with open(article_link_path, "w", encoding="utf-8") as f:
            f.write(f"{article_link_url}\npgc_id: {pgc_id}\n")
        logger.info(f"已写入文章链接: {article_link_path}")

        # --- 写入/更新日期汇总 ---
        # 从路径中解析 articles_published/{year}/{month}/{day}/article{N}
        pattern = r"articles_published[\\/](\d{4})[\\/](\d{2})[\\/](\d{2})[\\/]article(\d+)"
        m = re.search(pattern, article_dir)
        if not m:
            logger.warning(f"无法从 article_dir 解析日期结构，跳过日期汇总: {article_dir}")
            return

        year, month, day, idx = m.group(1), m.group(2), m.group(3), m.group(4)
        # articles_published 的父目录（即 D:\头条当日热点文章\articles_published）
        base_idx = article_dir.index("articles_published")
        base_dir = article_dir[:base_idx]
        date_dir = os.path.join(base_dir, "articles_published", year, month, day)
        os.makedirs(date_dir, exist_ok=True)
        summary_path = os.path.join(date_dir, "文章链接.txt")

        entry_line = f"{int(idx)}. [{title}]({article_link_url})"

        if os.path.isfile(summary_path):
            with open(summary_path, "r", encoding="utf-8") as f:
                existing = f.read()

            # 检查是否已有同编号条目 → 替换；否则追加
            prefix = f"{int(idx)}. "
            lines = existing.splitlines()
            replaced = False
            new_lines = []
            for line in lines:
                if line.startswith(prefix):
                    new_lines.append(entry_line)
                    replaced = True
                else:
                    new_lines.append(line)
            if not replaced:
                new_lines.append(entry_line)

            with open(summary_path, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
            logger.info(f"已{'更新' if replaced else '追加'}日期汇总: {summary_path}")
        else:
            date_label = f"{year}{month}{day}"
            header = f"# 头条当日热点文章 - {date_label}\n"
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(header + "\n" + entry_line + "\n")
            logger.info(f"已创建日期汇总: {summary_path}")

    # -- 诊断工具 --

    async def diagnose(self) -> dict:
        """
        诊断当前页面状态，帮助排查问题。

        返回页面中关键元素的存在情况。
        """
        logger.info("开始页面诊断...")

        checks = {
            "page_url": await self.eval_js("window.location.href"),
            "page_title": await self.eval_js("document.title"),
            "ready_state": await self.eval_js("document.readyState"),
        }

        # 检查各类元素
        element_checks = {
            "title_input": self.SELECTORS["title"],
            "editor": self.SELECTORS["editor"],
            "upload_btn": self.SELECTORS["image_upload_btn"],
            "file_input": self.SELECTORS["file_input"],
            "publish_btn": self.SELECTORS["publish_btn"],
        }

        for name, selectors in element_checks.items():
            found = []
            for sel in selectors:
                try:
                    exists = await self.eval_js(f"!!document.querySelector(`{sel}`)")
                    if exists:
                        found.append(sel)
                except Exception:
                    pass
            checks[name] = found if found else "未找到"

        # 额外信息
        checks["all_inputs"] = await self.eval_js("""
            (function() {
                var inputs = document.querySelectorAll('input, textarea');
                return Array.from(inputs).map(function(el) {
                    return {
                        tag: el.tagName,
                        type: el.type || '',
                        placeholder: el.placeholder || '',
                        className: el.className || '',
                        visible: el.offsetHeight > 0
                    };
                });
            })()
        """)

        checks["all_buttons"] = await self.eval_js("""
            (function() {
                var btns = document.querySelectorAll('button, [role="button"]');
                return Array.from(btns).map(function(el) {
                    return {
                        text: el.textContent.trim().substring(0, 30),
                        className: el.className || '',
                        visible: el.offsetHeight > 0
                    };
                });
            })()
        """)

        checks["contenteditable_elements"] = await self.eval_js("""
            (function() {
                var els = document.querySelectorAll('[contenteditable="true"]');
                return Array.from(els).map(function(el) {
                    return {
                        tag: el.tagName,
                        className: el.className || '',
                        id: el.id || '',
                        rect: el.getBoundingClientRect().toJSON()
                    };
                });
            })()
        """)

        checks["file_inputs_detail"] = await self.eval_js("""
            (function() {
                var inputs = document.querySelectorAll('input[type="file"]');
                return Array.from(inputs).map(function(el) {
                    return {
                        accept: el.accept || '',
                        multiple: el.multiple,
                        hidden: el.offsetHeight === 0 || el.style.display === 'none',
                        parentClass: el.parentElement ? el.parentElement.className : '',
                        id: el.id || '',
                        name: el.name || ''
                    };
                });
            })()
        """)

        logger.info("诊断结果:")
        for key, value in checks.items():
            logger.info(f"  {key}: {json.dumps(value, ensure_ascii=False)[:200]}")

        return checks


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="今日头条 CDP 图文发布工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 发布文章（含封面图）
  python toutiao_publisher.py publish \\
      --title "文章标题" \\
      --content article.html \\
      --images cover.jpg \\
      --auto-publish

  # 试运行（不实际发布）
  python toutiao_publisher.py publish \\
      --title "测试" \\
      --content "正文内容" \\
      --dry-run

  # 页面诊断
  python toutiao_publisher.py diagnose

  # 仅上传单张图片（测试上传功能）
  python toutiao_publisher.py upload-test --image test.jpg
        """,
    )

    parser.add_argument(
        "--host", default="localhost", help="CDP 调试地址 (默认: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=9222, help="CDP 调试端口 (默认: 9222)"
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # -- publish --
    pub = subparsers.add_parser("publish", help="发布文章")
    pub.add_argument("--title", required=True, help="文章标题")
    pub.add_argument(
        "--content",
        required=True,
        help="正文内容（直接文本）或 HTML/Markdown 文件路径",
    )
    pub.add_argument("--images", nargs="*", default=[], help="封面图片路径")
    pub.add_argument(
        "--content-images", nargs="*", default=[], help="正文内图片路径"
    )
    pub.add_argument(
        "--auto-publish",
        action="store_true",
        help="自动点击发布（否则仅准备内容）",
    )
    pub.add_argument(
        "--dry-run", action="store_true", help="试运行模式"
    )
    pub.add_argument(
        "--html", action="store_true", help="正文内容为 HTML 格式"
    )

    # -- diagnose --
    subparsers.add_parser("diagnose", help="诊断当前页面状态")

    # -- upload-test --
    up = subparsers.add_parser("upload-test", help="测试图片上传功能")
    up.add_argument("--image", required=True, help="要上传的图片路径")

    return parser


async def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    async with ToutiaoPublisher(host=args.host, port=args.port) as pub:
        if args.command == "publish":
            # 判断 content 是文件还是直接文本
            content = args.content
            is_html = args.html
            if os.path.isfile(content):
                with open(content, "r", encoding="utf-8") as f:
                    content = f.read()
                if content.endswith((".html", ".htm")):
                    is_html = True

            result = await pub.publish_article(
                title=args.title,
                content=content,
                image_paths=args.images,
                content_image_paths=args.content_images,
                is_html=is_html,
                auto_publish=args.auto_publish,
                dry_run=args.dry_run,
            )

            # 输出 JSON 结果
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "diagnose":
            result = await pub.diagnose()
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.command == "upload-test":
            success = await pub.upload_cover_image(args.image)
            print(json.dumps({"success": success}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
