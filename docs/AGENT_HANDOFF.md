# Agent 交接文档

本文档面向接手此项目的 AI agent，快速传达项目现状、核心技术要点和注意事项。

---

## 一、项目概述

这是一个**今日头条图文文章全自动发布系统**，通过 Chrome DevTools Protocol (CDP) 控制已登录的 Edge 浏览器，实现从内容填写到发布的全自动化。同时提供四项内容审查（文章质量、内容合规、图片质量、图片合规）、排版检查和热点话题管理能力。AI agent 通过 CLI 调用各工具脚本。

项目根目录：`D:\头条当日热点文章`

---

## 二、当前状态（截至 2026-06-13）

### 已完成且验证通过

| 功能                            | 状态                 | 关键文件                                                  |
| ------------------------------- | -------------------- | --------------------------------------------------------- |
| CDP 连接 Edge 浏览器            | 稳定                 | `tools/toutiao_publisher.py` → `CDPClient`                |
| 自动填写标题                    | 稳定                 | `ToutiaoPublisher.fill_title()`                           |
| 自动插入正文 HTML               | 稳定                 | `ToutiaoPublisher.insert_content()`                       |
| **剪贴板粘贴上传图片**          | **稳定（核心突破）** | `FileUploadHandler.upload_via_paste()`                    |
| 封面自动选择（三图模式）        | 稳定                 | `ToutiaoPublisher.select_cover_mode()`                    |
| 两步发布（预览并发布→确认发布） | 稳定                 | `ToutiaoPublisher.click_publish()`                        |
| 四项审查（文本+图片）           | 稳定                 | `text_reviewer.py`, `image_reviewer.py`, `full_review.py` |
| 排版检查                        | 稳定                 | `layout_checker.py`                                       |
| **热点话题数据层**              | **稳定**             | `hot_topic_manager.py`（相似度+历史扫描）                 |
| **每日发布报告索引**            | **稳定**             | `daily_report.py`（结构化日报供去重使用）                 |

### 已知问题 / 注意事项

1. **图片重复显示**：剪贴板粘贴后编辑器中可能出现 `img-loading-container` 残留元素（每个图片有 2 个 img 标签：一个 `pgc-img` 是实际的，一个 `img-loading-container` 是加载态），不影响发布。

2. **正文插入方式**：直接设置 `editor.innerHTML` 绕过了 ProseMirror 的内部状态管理。如果后续需要在插入内容后再做精细编辑（如光标操作），可能需要改用 ProseMirror 的 `dispatch(tr)` API。目前对发布流程无影响。

3. **Windows 编码问题**：脚本在 Windows 上运行时，含中文的 print 输出可能因 GBK 编码报错。解决方案：在脚本开头添加 `sys.stdout.reconfigure(encoding='utf-8', errors='replace')`。

4. **头条页面更新风险**：如果头条更新了前端结构（CSS 类名、组件结构），`SELECTORS` 配置和 JS 注入代码可能失效。用 `toutiao_diagnose_page` 工具可快速诊断。

---

## 三、核心技术要点

### 3.1 图片上传方案：ClipboardEvent 粘贴（已验证有效）

**这是整个项目最关键的技术突破。**

头条编辑器使用 ProseMirror，它内置了粘贴图片上传处理器。通过构造 `ClipboardEvent` 并携带 `File` 对象，可以触发编辑器的自动上传逻辑，图片会被上传到头条 CDN（`image-tt-private.toutiao.com`）。

```javascript
// 核心 JS 注入代码（在浏览器页面中执行）
var b64 = "<base64编码的图片数据>";
var binary = atob(b64);
var bytes = new Uint8Array(binary.length);
for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
var blob = new Blob([bytes], { type: "image/png" });
var file = new File([blob], "filename.png", { type: "image/png" });

var dt = new DataTransfer();
dt.items.add(file);

var pasteEvent = new ClipboardEvent("paste", {
  bubbles: true,
  cancelable: true,
  clipboardData: dt,
});
document.querySelector(".ProseMirror").dispatchEvent(pasteEvent);
```

**为什么其他方案失败：**

- CDP 文件选择器拦截（`Page.fileChooserOpened`）：工具栏图片按钮点击后不触发 `fileChooserOpened` 事件
- DOM `<input type="file">` 注入 + change 事件：文件被设置但编辑器的 React 事件处理器不响应
- 封面区域的添加按钮：打开的是图片库选择对话框，不是文件选择器

### 3.2 标题填写

头条标题输入框是 React 管理的 `<textarea>`，需要用 native setter 才能触发 React 的状态更新：

```javascript
var nativeSetter = Object.getOwnPropertyDescriptor(
  HTMLTextAreaElement.prototype,
  "value",
).set;
nativeSetter.call(textareaElement, title);
textareaElement.dispatchEvent(new Event("input", { bubbles: true }));
```

### 3.3 发布流程（两步）

1. 点击"预览并发布"按钮 → 页面切换到预览确认视图
2. 点击"确认发布"按钮 → 页面跳转到 `/profile_v4/graphic/articles`（文章管理页）

URL 跳转是判断发布成功的最可靠标志。

### 3.4 封面自动选择

粘贴正文图片后，头条编辑器会自动识别图片并填充封面区域。只需确保封面模式为"三图"（点击 `.byte-radio` 中文字为"三图"的元素）。

### 3.5 热点话题数据层与日报索引

**脚本定位**：`hot_topic_manager.py` 是纯数据层，不做话题分类和选题决策（那是大模型的活）。它只提供：历史文章列表、标题相似度数值、共享/独有关键词。

**日报索引**：`daily_report.py` 在每天发布完成后运行，扫描当天所有 article 目录，生成结构化的 `daily_report.json`。`hot_topic_manager` 的 `scan_history` 优先读取这份索引（速度快、信息全），找不到时降级到逐目录扫描。

日报包含每篇文章的：标题、URL、关键词(15个)、子标题(h2)、摘要(200字)、字数、图片数、发布时间。

`check_candidates` 的输出会透传这些元数据——当候选标题与历史文章有相似度时，comparisons 数组中会包含 `history_keywords`、`history_subheadings`、`history_excerpt`、`history_char_count`、`history_image_count`，让大模型不仅看到"标题有多像"，还能看到"之前那篇文章到底写了什么"。

**相似度算法**：jieba 关键词 Jaccard（权重 50%）+ 字符 bigram Jaccard（权重 50%）。综合两种粒度，对短标题有更好的区分能力。标题去重使用归一化处理（只保留中英文数字），避免引号/标点差异导致同一篇文章被重复计入。

**Agent 调用方式**（CLI）：

```bash
# 搜索热点后，检查候选话题（返回含历史文章摘要的 JSON）
python tools/hot_topic_manager.py check --candidates "话题A,话题B,话题C" --days 3

# 查看发布历史
python tools/hot_topic_manager.py history --days 7

# 生成当日日报（所有文章发布完成后运行）
python tools/daily_report.py --date 20260613
```

---

## 四、目录结构

```
D:\头条当日热点文章\
├── tools/                          # 核心工具库
│   ├── toutiao_publisher.py        # [核心] CDP 发布模块
│   │   ├── CDPClient               #   WebSocket CDP 客户端
│   │   ├── FileUploadHandler       #   文件上传（含 upload_via_paste）
│   │   └── ToutiaoPublisher        #   发布器（完整流程编排）
│   ├── auto_publish_final.py       # 独立可运行的最终发布脚本
│   ├── hot_topic_manager.py        # 热点话题数据层（相似度计算、历史扫描）
│   ├── daily_report.py             # [新] 每日发布报告生成器
│   ├── text_reviewer.py            # 文章质量 + 内容合规审查
│   ├── image_reviewer.py           # 图片质量 + 图片合规审查
│   ├── full_review.py              # 四项审查统一入口
│   ├── layout_checker.py           # CDP 排版检查
│   ├── pre_publish_workflow.py     # 审查→填写→检查→发布 工作流
│   ├── publish_config.json         # 发布配置（含去重规则、日报流程）
│   ├── requirements.txt            # Python 依赖
│   └── config/
│       └── sensitive_words_sample.json  # 敏感词库模板
│
├── docs/
│   ├── 使用说明.md                 # 用户文档
│   ├── AGENT_HANDOFF.md            # 本文档（Agent 交接）
│   └── 文章发布操作和要求说明书-必读.txt  # 原始需求
│
├── articles_published/              # 已发布文章（按年/月/日层级归档）
│   └── {year}/
│       └── {month}/
│           └── {day}/               # 如 2026/06/13
│               ├── daily_report.json   # 当日结构化发布报告索引
│               ├── 文章链接.txt        # 当日所有文章汇总（编号. [标题](链接)）
│               ├── article1/
│               │   ├── article1.txt    # 正文（文件名=目录名）
│               │   ├── cover.jpg
│               │   ├── img_01.jpg
│               │   └── 文章链接.txt    # 单篇链接
│               └── article2/ ...
│                   ├── article2.txt
│                   ├── ...
│
└── .credentials/                   # 账号凭证（已在 .gitignore）
```

---

## 五、CLI 工具调用清单

Agent 通过 `python <script> [args]` 调用以下工具：

| 脚本                            | 功能                   | 关键参数                                             |
| ------------------------------- | ---------------------- | ---------------------------------------------------- |
| `hot_topic_manager.py check`    | 候选话题相似度检查     | `--candidates "话题1,话题2"`, `--days 3`             |
| `hot_topic_manager.py history`  | 查看发布历史           | `--days 7`                                           |
| `daily_report.py`               | 生成当日结构化发布报告 | `--date YYYYMMDD`（默认今天）                        |
| `auto_publish_final.py`         | 全自动发布文章         | `--title`, `--content`, `--images`, `--cover-mode`   |
| `full_review.py`                | 四项全面审查           | `--title`, `--content`, `--images`                   |
| `text_reviewer.py`              | 文本审查（质量+合规）  | `--title`, `--content`                               |
| `image_reviewer.py`             | 图片审查（质量+合规）  | 图片路径列表                                         |
| `layout_checker.py`             | 排版对齐检查           | 无（检查当前编辑器页面）                             |
| `toutiao_publisher.py diagnose` | 页面元素诊断           | 无                                                   |
| `pre_publish_workflow.py`       | 完整发布工作流         | `--title`, `--content`, `--images`, `--auto-publish` |

---

## 六、典型发布流程（给 Agent 的参考）

一次完整的发布任务必须包含以下步骤，按顺序执行：

```bash
# === 阶段 1：选题与去重 ===
# 搜索当日热点 → 跑 hot_topic_manager 去重 → 人工选定话题
python tools/hot_topic_manager.py check \
    --candidates "话题A,话题B,话题C" \
    --days 3


# === 阶段 2：写文与配图 ===
# 确定当天编号（扫描 {day}/ 下已有 article{N}/，取 max(N)+1）
# 今天第一篇文章 → article1
# 正文写入 {day}/article{N}/article{N}.txt（文件名=目录名）
# 生成 3-4 张配图放入同一目录


# === 阶段 3：审查与发布 ===
# 启动 Edge CDP（如未启动）
msedge.exe --remote-debugging-port=9222

# 发布（pre_publish_workflow 内部执行审查→填写→发布）
python tools/pre_publish_workflow.py \
    --title "文章标题" \
    --content "D:\...\article{N}\article{N}.txt" \
    --images cover.jpg img1.jpg img2.jpg \
    --auto-publish

# 如 pre_publish_workflow 未能实际发布，查看原因，修复，循环这个过程直到通过并成功发布为止
python tools/toutiao_publisher.py publish \
    --title "文章标题" \
    --content "D:\...\article{N}\article{N}.txt" \
    --images cover.jpg \
    --content-images img1.jpg img2.jpg img3.jpg \
    --auto-publish


# === 阶段 4：收尾产物（发布成功后必须执行） ===
# 4a. 创建/更新当日汇总链接文件
#    路径: {day}/文章链接.txt
#    格式: 编号. [标题](https://www.toutiao.com/item/{pgc_id}/)

# 4b. 生成日报索引
python tools/daily_report.py --date `$(date +年\月\日)`
```

---

## 七、发布流程强制规范（AI Agent 必读，违反即错）

> **本节优先级最高。以下每一条都是历史踩坑血泪教训，任何一条未遵守都算任务失败。**

### 7.1 目录与文件命名硬规则

| 规则       | 说明                                         | 错误示例                   | 正确示例                  |
| ---------- | -------------------------------------------- | -------------------------- | ------------------------- |
| 日期目录   | 始终使用**当天日期**动态计算，**禁止硬编码** | `2026\06\13`（硬编码昨日） | `$(date +年\月\日)`       |
| 文章编号   | **每日从 1 开始重置**，不跨天累计            | `article17`（续昨天编号）  | `article1`（当天第一篇）  |
| 正文文件名 | 必须等于 `article{N}.txt`，与所在目录名一致  | `article1/article17.txt`   | `article1/article1.txt`   |
| 图片文件名 | 不限格式，放在 article{N} 目录下即可         | —                          | `cover.jpg`, `img_01.png` |

**判断当天编号**：扫描 `articles_published/{year}/{month}/{day}/` 下已有 `article{N}/` 子目录，取 `max(N)+1`。

### 7.2 每日必需产物清单

每天第一篇文章发布后，**必须**创建以下文件，缺一不可：

| 文件             | 路径                              | 格式                                                       | 谁负责     |
| ---------------- | --------------------------------- | ---------------------------------------------------------- | ---------- |
| 文章正文         | `{day}/article{N}/article{N}.txt` | Markdown/纯文本                                            | 写文者     |
| 配图(3-4张)      | `{day}/article{N}/*.jpg`          | JPEG                                                       | 配图者     |
| 单篇链接         | `{day}/article{N}/文章链接.txt`   | `标题 + pgc_id`                                            | 发布者     |
| **当日汇总链接** | `{day}/文章链接.txt`              | `# 头条当日热点文章 - YYYYMMDD\n\n1. [标题](链接)\n2. ...` | **发布者** |
| 日报索引         | `{day}/daily_report.json`         | JSON（由 `daily_report.py` 生成）                          | 发布者     |

**当日汇总链接格式**（严格参考）：

```
# 头条当日热点文章 - 20260614

1. [文章标题](https://www.toutiao.com/item/pgc_id/)
2. [文章标题](https://www.toutiao.com/item/pgc_id/)
```

- 编号从 1 开始递增
- 每条一行 `编号. [标题](链接)`
- 链接格式：`https://www.toutiao.com/item/{pgc_id}/`

### 7.3 发布任务中禁止的行为

- ❌ 在 task 参数中硬编码日期路径（写 `2026\06\13`）
- ❌ 跨天累加文章编号
- ❌ 文件名和目录名不一致
- ❌ 忘记创建 `文章链接.txt`（当日汇总）
- ❌ 发布成功后不运行 `daily_report.py` 更新索引
- ❌ 用 `pre_publish_workflow` 的 `present_result` 方式等待用户确认后再发布（应直接 `--auto-publish`）

### 7.4 发布完成后的自检清单

发布完成后逐项确认：

1. [ ] 目录名是 `article{N}`（当天从1起编）
2. [ ] 正文文件名是 `article{N}.txt`
3. [ ] 当日汇总 `文章链接.txt` 已创建或更新
4. [ ] `daily_report.json` 已更新
5. [ ] 头条文章链接可访问

## 八、环境要求

- **Python** 3.10+
- **Edge 浏览器**已通过 `--remote-debugging-port=9222` 启动
- **已登录**头条创作平台 `https://mp.toutiao.com`
- **依赖**：`pip install -r tools/requirements.txt`
  - 核心：`websockets`, `mcp[cli]`
  - 审查：`Pillow`, `numpy`, `jieba`
  - 可选：`opencv-python`（更好的模糊检测）, `pytesseract`（OCR）

---

## 八、原始需求摘要

用户要求：

- 发布正常的文字，带上 3-4 张图，结合当日热点，做合理评论
- **全自动**，不接受任何手动步骤
- 标题限制 2-30 字
- 内容不能违规，需要有一定深度
- 图片和文字排版需要对应
- 不能和历史文章重复

发布页面：`https://mp.toutiao.com/profile_v4/graphic/publish`
文章列表：`https://mp.toutiao.com/profile_v4/graphic/articles`
