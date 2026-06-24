#!/usr/bin/env python3
"""
四项全面审查：文章质量 + 内容合规 + 图片质量 + 图片合规

发布前一站式审查工具，串联 text_reviewer 和 image_reviewer，
输出统一的审查报告和通过/未通过判定。

使用方式:
  # CLI
  python full_review.py \
      --title "文章标题" \
      --content "正文内容" \
      --images cover.jpg img1.jpg img2.jpg

  # 从文件读取正文
  python full_review.py \
      --title "标题" \
      --content article.html \
      --images *.jpg

  # JSON 输出
  python full_review.py --title "标题" --content "内容" --json

  # 接入已有的预发布工作流 (在 pre_publish_workflow.py 中 import)
  from full_review import full_review
  result = full_review(title, content, image_paths)
"""

import json
import sys
import os
import argparse
import logging
from typing import Optional

from text_reviewer import review_text
from image_reviewer import review_images

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("full_review")


def full_review(
    title: str,
    content: str,
    image_paths: Optional[list[str]] = None,
    word_db_file: Optional[str] = None,
) -> dict:
    """
    执行四项全面审查。

    Args:
        title:        文章标题
        content:      文章正文（HTML 或纯文本）
        image_paths:  图片文件路径列表
        word_db_file: 自定义敏感词库 JSON

    Returns:
        {
            "article_quality":   { score, summary, stats, issues },
            "content_compliance": { score, summary, stats, issues },
            "image_quality":     { score, summary, issues, per_image },
            "image_compliance":  { score, summary, issues, per_image },
            "overall_score":     int,
            "overall_passed":    bool,
            "overall_summary":   str,
            "blocking_issues":   [...],   # 阻塞发布的严重问题
        }
    """
    results = {}

    # ---- 文本审查 ----
    logger.info("正在执行文本审查（文章质量 + 内容合规）...")
    text_result = review_text(title, content, word_db_file=word_db_file)
    results["article_quality"] = text_result["article_quality"]
    results["content_compliance"] = text_result["content_compliance"]

    # ---- 图片审查 ----
    if image_paths and len(image_paths) > 0:
        logger.info(f"正在执行图片审查（{len(image_paths)} 张图片）...")
        img_result = review_images(image_paths)
        results["image_quality"] = img_result["image_quality"]
        results["image_compliance"] = img_result["image_compliance"]
    else:
        logger.info("无图片，跳过图片审查")
        results["image_quality"] = {"score": 100, "summary": "无图片", "issues": [], "per_image": {}}
        results["image_compliance"] = {"score": 100, "summary": "无图片", "issues": [], "per_image": {}}

    # ---- 综合评分 ----
    scores = [
        results["article_quality"]["score"],
        results["content_compliance"]["score"],
        results["image_quality"]["score"],
        results["image_compliance"]["score"],
    ]
    overall_score = min(scores)

    # 收集所有阻塞问题（error 级别）
    blocking = []
    for section in results.values():
        if isinstance(section, dict) and "issues" in section:
            for issue in section["issues"]:
                if issue.get("severity") == "error":
                    blocking.append(issue)

    passed = len(blocking) == 0 and overall_score >= 50

    # 总评
    section_labels = {
        "article_quality": "文章质量",
        "content_compliance": "内容合规",
        "image_quality": "图片质量",
        "image_compliance": "图片合规",
    }
    score_lines = ", ".join(
        f"{label}={results[key]['score']}"
        for key, label in section_labels.items()
    )

    if passed:
        summary = f"审查通过（综合={overall_score}/100）: {score_lines}"
    else:
        summary = f"审查未通过（综合={overall_score}/100，{len(blocking)}个阻塞问题）: {score_lines}"

    results["overall_score"] = overall_score
    results["overall_passed"] = passed
    results["overall_summary"] = summary
    results["blocking_issues"] = blocking

    return results


# ===========================================================================
# 格式化输出
# ===========================================================================
def format_full_report(result: dict) -> str:
    """将完整审查结果格式化为可读文本。"""
    lines = []
    lines.append("╔══════════════════════════════════════════════════════════╗")
    lines.append("║          今日头条文章发布前四项全面审查报告          ║")
    lines.append("╚══════════════════════════════════════════════════════════╝")
    lines.append("")

    # 总览
    status = "✓ 通过" if result["overall_passed"] else "✗ 未通过"
    lines.append(f"  综合结果: {status}    评分: {result['overall_score']}/100")
    lines.append("")

    # 四项评分一览
    sections = [
        ("article_quality", "文章质量", "📝"),
        ("content_compliance", "内容合规", "🔍"),
        ("image_quality", "图片质量", "🖼"),
        ("image_compliance", "图片合规", "🛡"),
    ]

    lines.append("  ┌──────────────┬────────┬──────────────────────────────────┐")
    lines.append("  │    审查项     │  评分  │              摘要               │")
    lines.append("  ├──────────────┼────────┼──────────────────────────────────┤")
    for key, label, icon in sections:
        s = result.get(key, {})
        score = s.get("score", "N/A")
        summary = s.get("summary", "")[:35]
        issue_count = len(s.get("issues", []))
        tag = "" if issue_count == 0 else f" ({issue_count}项)"
        lines.append(f"  │ {icon} {label:<10} │ {score:>4}  │ {summary:<32}{tag} │")
    lines.append("  └──────────────┴────────┴──────────────────────────────────┘")
    lines.append("")

    # 阻塞问题
    blocking = result.get("blocking_issues", [])
    if blocking:
        lines.append("  ⚠ 阻塞发布的严重问题（必须修复）:")
        lines.append("  " + "-" * 50)
        for idx, issue in enumerate(blocking, 1):
            cat = issue.get("category", "")
            msg = issue.get("message", "")
            sug = issue.get("suggestion", "")
            lines.append(f"    {idx}. [{cat}] {msg}")
            if sug:
                lines.append(f"       → {sug}")
        lines.append("")

    # 各审查项详情（只显示有问题的）
    for key, label, icon in sections:
        s = result.get(key, {})
        issues = s.get("issues", [])
        if not issues:
            continue

        lines.append(f"  {'─'*55}")
        lines.append(f"  {icon} {label}详情 (评分 {s['score']}/100)")
        lines.append(f"  {'─'*55}")

        for severity, sl in [("error", "✗ 错误"), ("warning", "! 警告"), ("info", "· 建议")]:
            cat_issues = [i for i in issues if i.get("severity") == severity]
            if not cat_issues:
                continue
            for i in cat_issues:
                cat = i.get("category", "")
                msg = i.get("message", "")
                ctx = i.get("context", "")
                sug = i.get("suggestion", "")
                lines.append(f"    {sl}  [{cat}] {msg}")
                if ctx:
                    lines.append(f"         上下文: {ctx[:80]}")
                if sug:
                    lines.append(f"         建议: {sug}")

        lines.append("")

    # 统计
    stats = result.get("article_quality", {}).get("stats", {})
    if stats:
        lines.append(f"  {'─'*55}")
        lines.append("  文章统计:")
        for k, v in stats.items():
            lines.append(f"    {k}: {v}")
        lines.append("")

    lines.append("╔══════════════════════════════════════════════════════════╗")
    if result["overall_passed"]:
        lines.append("║  ✓ 审查通过，文章可以发布                          ║")
    else:
        lines.append("║  ✗ 审查未通过，请修复上述问题后重新审查              ║")
    lines.append("╚══════════════════════════════════════════════════════════╝")

    return "\n".join(lines)


# ===========================================================================
# CLI
# ===========================================================================
def main():
    parser = argparse.ArgumentParser(
        description="今日头条文章四项全面审查",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 全量审查
  python full_review.py \\
      --title "文章标题" \\
      --content "正文内容或文件路径" \\
      --images cover.jpg img1.jpg

  # JSON 输出（供 agent 消费）
  python full_review.py --title "标题" --content "内容" --json

  # 使用自定义敏感词库
  python full_review.py --title "标题" --content "内容" --word-db my_words.json

退出码:
  0 = 审查通过
  1 = 审查未通过
        """,
    )
    parser.add_argument("--title", required=True, help="文章标题")
    parser.add_argument("--content", required=True, help="正文内容（文本或文件路径）")
    parser.add_argument("--images", nargs="*", default=[], help="图片文件路径")
    parser.add_argument("--word-db", help="自定义敏感词库 JSON")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--output", "-o", help="将报告写入文件")
    args = parser.parse_args()

    # 读取内容
    content = args.content
    if os.path.isfile(content):
        with open(content, "r", encoding="utf-8") as f:
            content = f.read()

    # 展开图片路径中的 glob
    image_paths = []
    for p in args.images:
        import glob
        expanded = glob.glob(p)
        if expanded:
            image_paths.extend(expanded)
        elif os.path.isfile(p):
            image_paths.append(p)
        else:
            logger.warning(f"图片不存在: {p}")

    # 执行审查
    result = full_review(
        title=args.title,
        content=content,
        image_paths=image_paths,
        word_db_file=args.word_db,
    )

    # 输出
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_full_report(result))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"报告已写入: {args.output}")

    sys.exit(0 if result["overall_passed"] else 1)


if __name__ == "__main__":
    main()
