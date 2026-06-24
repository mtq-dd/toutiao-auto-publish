#!/usr/bin/env python3
"""
预发布工作流：内容审查 → 图片审查 → 排版检查 → 发布

演示如何将四项审查与发布工具串联使用：
  Step 0  四项全面审查（文章质量 + 内容合规 + 图片质量 + 图片合规）
  Step 1  填写标题和正文（dry-run 模式，不发布）
  Step 2  排版 & 图片对齐检查（通过 CDP 检查页面渲染）
  Step 3  检查通过 → 自动发布；不通过 → 中止并报告

使用方式:
  python pre_publish_workflow.py \
      --title "文章标题" \
      --content article.html \
      --images cover.jpg img1.jpg \
      --port 9222 \
      --auto-publish
"""

import asyncio
import argparse
import json
import logging
import sys
import os
from datetime import datetime

from full_review import full_review, format_full_report
from layout_checker import LayoutChecker, format_report_text
from toutiao_publisher import ToutiaoPublisher
from daily_report import DailyReportGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("workflow")


async def run_workflow(
    title: str,
    content: str,
    image_paths: list[str],
    host: str = "localhost",
    port: int = 9222,
    is_html: bool = True,
    auto_publish: bool = False,
    word_db_file: str = None,
    mark_ai_generated: bool = False,
    article_dir: str = None,
):
    """
    预发布工作流：
      Step 0  四项全面审查（离线分析，不依赖浏览器）
      Step 1  填写标题和正文（dry-run 模式，不发布）
      Step 2  排版检查（CDP 检查页面渲染效果）
      Step 3  全部通过 → 发布
    """

    # ---- Step 0: 四项全面审查 ----
    logger.info("=" * 50)
    logger.info("Step 0: 四项全面审查（质量 + 合规 + 图片）")
    logger.info("=" * 50)

    review_result = full_review(
        title=title,
        content=content,
        image_paths=image_paths,
        word_db_file=word_db_file,
    )

    print(format_full_report(review_result))

    if not review_result["overall_passed"]:
        logger.error("四项审查未通过，中止发布流程")
        logger.error(f"阻塞问题: {len(review_result['blocking_issues'])} 个")
        for i, issue in enumerate(review_result["blocking_issues"], 1):
            logger.error(f"  {i}. [{issue['category']}] {issue['message']}")
        return {
            "success": False,
            "reason": "四项审查未通过",
            "review": review_result,
        }

    logger.info(f"四项审查通过（评分 {review_result['overall_score']}/100），继续...")

    # ---- Step 1: 填写内容 ----
    logger.info("=" * 50)
    logger.info("Step 1: 填写文章内容（试运行模式）")
    logger.info("=" * 50)

    async with ToutiaoPublisher(host=host, port=port) as pub:
        fill_result = await pub.publish_article(
            title=title,
            content=content,
            content_image_paths=image_paths,
            is_html=is_html,
            dry_run=True,
            auto_publish=False,
        )

    if not fill_result.get("title_ok") or not fill_result.get("content_ok"):
        logger.error("内容填写失败，中止流程")
        logger.error(json.dumps(fill_result, ensure_ascii=False, indent=2))
        return {"success": False, "reason": "内容填写失败", "fill_result": fill_result}

    logger.info("内容填写完成，进入排版检查...")
    await asyncio.sleep(2)

    # ---- Step 2: 排版检查 ----
    logger.info("=" * 50)
    logger.info("Step 2: 排版 & 图片对齐检查（CDP）")
    logger.info("=" * 50)

    async with LayoutChecker(host=host, port=port) as checker:
        layout_report = await checker.run_all_checks()

    print(format_report_text(layout_report))

    layout_errors = [i for i in layout_report.issues if i.severity == "error"]
    if layout_errors:
        logger.error(f"排版检查未通过：{len(layout_errors)} 个错误")
        return {
            "success": False,
            "reason": "排版检查未通过",
            "review": review_result,
            "layout_report": layout_report.to_dict(),
        }

    logger.info(f"排版检查通过，评分 {layout_report.score}/100")

    # ---- Step 3: 发布 ----
    if auto_publish:
        logger.info("=" * 50)
        logger.info("Step 3: 正式发布")
        logger.info("=" * 50)

        async with ToutiaoPublisher(host=host, port=port) as pub:
            publish_result = await pub.publish_article(
                title=title,
                content=content,
                content_image_paths=image_paths,
                is_html=is_html,
                auto_publish=True,
                mark_ai_generated=mark_ai_generated,
                article_dir=article_dir,
            )

        logger.info("发布完成！")

        # ---- Step 4: 更新/生成 daily_report.json ----
        daily_report_path = None
        try:
            date_str = datetime.now().strftime("%Y%m%d")
            gen = DailyReportGenerator()
            report = gen.generate(date_str)
            if "error" not in report:
                daily_report_path = gen.save(report)
                logger.info(f"日报已更新: {daily_report_path} (共 {report['article_count']} 篇)")
            else:
                logger.warning(f"日报生成跳过: {report['error']}")
        except Exception as e:
            logger.warning(f"日报生成失败: {e}")

        # ---- Step 5: 修复编辑器自动追加的声明文本 ----
        if daily_report_path:
            import time as _time
            _time.sleep(5)
            try:
                raw = daily_report_path.read_text(encoding="utf-8")
                if "仅供参考" in raw:
                    tail = raw.rfind('}')
                    if tail >= 0:
                        clean = raw[:tail + 1] + '\n'
                        daily_report_path.write_text(clean, encoding="utf-8")
                        logger.info("已修复 daily_report.json（移除编辑器自动追加的声明）")
            except Exception as e:
                logger.warning(f"daily_report 修复失败: {e}")

        return {
            "success": True,
            "review_score": review_result["overall_score"],
            "layout_score": layout_report.score,
            "review": review_result,
            "layout_report": layout_report.to_dict(),
            "publish_result": publish_result,
        }
    else:
        logger.info("=" * 50)
        logger.info("全部检查通过！请在浏览器中手动确认并发布")
        logger.info("=" * 50)
        return {
            "success": True,
            "review_score": review_result["overall_score"],
            "layout_score": layout_report.score,
            "review": review_result,
            "layout_report": layout_report.to_dict(),
            "message": "请在浏览器中手动点击发布",
        }


def main():
    parser = argparse.ArgumentParser(
        description="预发布工作流：四项审查 → 排版检查 → 发布",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--title", required=True, help="文章标题")
    parser.add_argument("--content", required=True, help="正文（文本或文件路径）")
    parser.add_argument("--images", nargs="*", default=[], help="封面/正文图片")
    parser.add_argument("--host", default="localhost", help="CDP 地址")
    parser.add_argument("--port", type=int, default=9222, help="CDP 端口")
    parser.add_argument("--html", action="store_true", help="正文为 HTML 格式")
    parser.add_argument("--auto-publish", action="store_true", help="全部检查通过后自动发布")
    parser.add_argument("--word-db", help="自定义敏感词库 JSON 文件")
    parser.add_argument("--mark-ai-generated", action="store_true", help="勾选「引用AI」作品声明")
    parser.add_argument("--output", "-o", help="将结果写入 JSON 文件")

    args = parser.parse_args()

    content = args.content
    content_file_path = None
    if os.path.isfile(content):
        content_file_path = content
        with open(content, "r", encoding="utf-8") as f:
            content = f.read()

    # 从 content 文件路径推导 article_dir
    article_dir = None
    if content_file_path:
        article_dir = os.path.dirname(content_file_path)

    result = asyncio.run(run_workflow(
        title=args.title,
        content=content,
        image_paths=args.images,
        host=args.host,
        port=args.port,
        is_html=args.html,
        auto_publish=args.auto_publish,
        word_db_file=args.word_db,
        mark_ai_generated=args.mark_ai_generated,
        article_dir=article_dir,
    ))

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    # 输出 JSON 摘要
    print(json.dumps({
        "success": result.get("success"),
        "review_score": result.get("review_score", "N/A"),
        "layout_score": result.get("layout_score", "N/A"),
        "reason": result.get("reason", ""),
    }, ensure_ascii=False, indent=2))

    sys.exit(0 if result.get("success") else 1)


if __name__ == "__main__":
    main()
