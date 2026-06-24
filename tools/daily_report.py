#!/usr/bin/env python3
"""
每日发布报告生成器

扫描指定日期目录，生成结构化的 daily_report.json。
这份报告是 hot_topic_manager 的数据源，也是给 agent 的历史上下文。

每天发布完成后运行一次：
  python daily_report.py                    # 生成今天的
  python daily_report.py --date 20260612    # 补生成指定日期的

输出文件：{base_dir}/articles_published/{year}/{month}/{day}/daily_report.json
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class DailyReportGenerator:

    def __init__(self, base_dir: str = r"D:\头条当日热点文章"):
        self.base_dir = Path(base_dir)
        self.articles_root = self.base_dir / "articles_published"
        self._jieba_loaded = False
        self._jieba_analyse = None

    @staticmethod
    def _date_str_to_parts(date_str: str) -> tuple[str, str, str]:
        """将 YYYYMMDD 拆为 (year, month, day)"""
        return date_str[:4], date_str[4:6], date_str[6:]

    def _date_dir(self, date_str: str) -> Path:
        """根据日期字符串返回文章目录路径: articles_published/{year}/{month}/{day}"""
        y, m, d = self._date_str_to_parts(date_str)
        return self.articles_root / y / m / d

    def _ensure_jieba(self):
        if not self._jieba_loaded:
            try:
                import jieba.analyse
                self._jieba_analyse = jieba.analyse
            except ImportError:
                self._jieba_analyse = None
            self._jieba_loaded = True

    def _extract_keywords(self, text: str, topk: int = 15) -> list[str]:
        self._ensure_jieba()
        if self._jieba_analyse:
            return self._jieba_analyse.extract_tags(text, topK=topk)
        return [w for w in re.findall(r'[\u4e00-\u9fff]{2,}', text) if len(w) >= 2][:topk]

    def _parse_html(self, path: Path) -> dict:
        """从 content.html 提取元数据"""
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return {}

        # 标题
        title = ""
        m = re.search(r'<title[^>]*>(.*?)</title>', text, re.DOTALL | re.IGNORECASE)
        if m:
            title = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        if not title:
            m = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.DOTALL | re.IGNORECASE)
            if m:
                title = re.sub(r'<[^>]+>', '', m.group(1)).strip()

        # h2 子标题（文章结构）
        h2s = re.findall(r'<h2[^>]*>(.*?)</h2>', text, re.DOTALL | re.IGNORECASE)
        subheadings = [re.sub(r'<[^>]+>', '', h).strip() for h in h2s if h.strip()]

        # 纯文本（先去 style/script 标签，再去 HTML 标签）
        clean = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
        plain = re.sub(r'<[^>]+>', '', clean)
        plain = re.sub(r'\s+', ' ', plain).strip()

        # 字数
        char_count = len(re.sub(r'\s', '', plain))

        # 图片数
        img_count = len(re.findall(r'<img[^>]+>', text, re.IGNORECASE))

        # 摘要（前 200 字）
        excerpt = plain[:200] + ("..." if len(plain) > 200 else "")

        # 关键词（从全文提取）
        keywords = self._extract_keywords(plain, topk=15)

        return {
            "title": title,
            "subheadings": subheadings,
            "char_count": char_count,
            "image_count": img_count,
            "excerpt": excerpt,
            "keywords": keywords,
        }

    def _parse_link_file(self, path: Path) -> list[dict]:
        """从 文章链接.txt 提取标题和 URL"""
        articles = []
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return articles
        for line in text.strip().splitlines():
            m = re.match(r'^[\d\-\*\.]+\s*\[([^\]]+)\]\(([^)]+)\)', line.strip())
            if m:
                articles.append({"title": m.group(1), "url": m.group(2)})
        return articles

    def _get_dir_mtime(self, dir_path: Path) -> Optional[str]:
        """获取目录中最近修改文件的时间"""
        latest = 0
        for f in dir_path.iterdir():
            if f.is_file():
                mt = f.stat().st_mtime
                if mt > latest:
                    latest = mt
        if latest > 0:
            return datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M:%S")
        return None

    def generate(self, date_str: str) -> dict:
        """生成指定日期的发布报告"""
        date_dir = self._date_dir(date_str)
        if not date_dir.is_dir():
            return {"error": f"目录不存在: {date_str} (路径: {date_dir})", "articles": []}

        # 先读 文章链接.txt 获取 URL 映射
        link_file = date_dir / "文章链接.txt"
        url_map = {}
        if link_file.is_file():
            for item in self._parse_link_file(link_file):
                url_map[item["title"]] = item["url"]

        # 扫描 article 子目录
        articles = []
        article_dirs = sorted([
            d for d in date_dir.iterdir()
            if d.is_dir() and d.name.startswith("article")
        ], key=lambda d: int(re.search(r'\d+', d.name).group()) if re.search(r'\d+', d.name) else 0)

        for art_dir in article_dirs:
            article_num = re.search(r'\d+', art_dir.name)
            num = int(article_num.group()) if article_num else 0

            html_file = art_dir / "content.html"
            if not html_file.is_file():
                continue

            info = self._parse_html(html_file)
            if not info.get("title"):
                continue

            # 匹配 URL
            url = url_map.get(info["title"], "")
            if not url:
                # 模糊匹配（标题可能有微小差异）
                for linked_title, linked_url in url_map.items():
                    if info["title"][:10] == linked_title[:10]:
                        url = linked_url
                        break

            # 图片文件列表
            img_dir = art_dir / "images"
            image_files = []
            if img_dir.is_dir():
                image_files = sorted([
                    f.name for f in img_dir.iterdir()
                    if f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp')
                ])

            # 发布时间（从文件修改时间推断）
            publish_time = self._get_dir_mtime(art_dir)

            articles.append({
                "seq": num,
                "title": info["title"],
                "url": url,
                "subheadings": info["subheadings"],
                "char_count": info["char_count"],
                "image_count": max(info["image_count"], len(image_files)),
                "keywords": info["keywords"],
                "excerpt": info["excerpt"],
                "publish_time": publish_time,
                "dir": art_dir.name,
            })

        # 补充：文章链接.txt 中有但目录里没匹配到的
        # 用归一化标题去重，避免引号/标点差异导致重复
        def normalize(t):
            return re.sub(r'[^\u4e00-\u9fffa-zA-Z0-9]', '', t).lower()

        existing_normalized = {normalize(a["title"]) for a in articles}
        for linked_title, linked_url in url_map.items():
            norm_linked = normalize(linked_title)
            if norm_linked in existing_normalized:
                continue
            # 模糊匹配：检查是否任一已有文章的归一化标题包含当前标题的核心词
            is_dup = False
            for existing_norm in existing_normalized:
                # 取较短标题的前8个字符作为核心片段进行匹配
                core = min(len(norm_linked), len(existing_norm), 8)
                if core >= 4 and norm_linked[:core] == existing_norm[:core]:
                    is_dup = True
                    break
            if is_dup:
                continue
                articles.append({
                    "seq": len(articles) + 1,
                    "title": linked_title,
                    "url": linked_url,
                    "subheadings": [],
                    "char_count": 0,
                    "image_count": 0,
                    "keywords": self._extract_keywords(linked_title, topk=8),
                    "excerpt": "",
                    "publish_time": None,
                    "dir": None,
                })
                existing_normalized.add(normalize(linked_title))

        # 按序号排序
        articles.sort(key=lambda a: a["seq"])

        report = {
            "date": date_str,
            "date_display": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
            "article_count": len(articles),
            "articles": articles,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        return report

    def save(self, report: dict) -> Path:
        """保存报告到日期目录"""
        date_str = report["date"]
        out_dir = self._date_dir(date_str)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "daily_report.json"
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out_path


def main():
    parser = argparse.ArgumentParser(description="每日发布报告生成器")
    parser.add_argument("--base-dir", default=r"D:\头条当日热点文章")
    parser.add_argument("--date", default=None,
                       help="日期 YYYYMMDD（默认今天）")
    args = parser.parse_args()

    gen = DailyReportGenerator(base_dir=args.base_dir)

    date_str = args.date or datetime.now().strftime("%Y%m%d")
    print(f"生成日报: {date_str}")

    report = gen.generate(date_str)
    if "error" in report:
        print(f"错误: {report['error']}")
        return

    out = gen.save(report)
    print(f"已保存: {out}")
    print(f"文章数: {report['article_count']}")

    for a in report["articles"]:
        print(f"  [{a['seq']:>2}] {a['title']}")
        print(f"       {a['char_count']}字 | {a['image_count']}图 | "
              f"关键词: {', '.join(a['keywords'][:5])}")
        if a['subheadings']:
            print(f"       结构: {' → '.join(a['subheadings'][:3])}")


if __name__ == "__main__":
    main()
