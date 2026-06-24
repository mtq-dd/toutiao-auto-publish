#!/usr/bin/env python3
"""
热点话题管理 —— 数据层

本脚本不做话题分类和选题决策（那是大模型的活）。
它只做三件事：
  1. 扫描历史：最近 N 天发了哪些文章（标题+链接+日期）
  2. 计算相似度：候选标题 vs 历史标题的客观数值
  3. 提取关键词：让大模型看到两个标题共享了什么词

大模型拿到这些数据后自行判断：是延续、重复还是全新话题。

CLI 用法：
  # 查看最近发布历史
  python hot_topic_manager.py history --days 3

  # 计算候选标题与历史的相似度
  python hot_topic_manager.py check --candidates "话题A,话题B,话题C" --days 3
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class PublishedArticle:
    title: str
    date: str           # YYYYMMDD
    url: str = ""
    article_dir: str = ""
    # 以下字段来自 daily_report.json，降级扫描时为空
    keywords: list = field(default_factory=list)
    subheadings: list = field(default_factory=list)
    excerpt: str = ""
    char_count: int = 0
    image_count: int = 0


# ---------------------------------------------------------------------------
# 核心
# ---------------------------------------------------------------------------

class HotTopicManager:

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

    def _extract_keywords(self, text: str, topk: int = 10) -> list[str]:
        self._ensure_jieba()
        if self._jieba_analyse:
            return self._jieba_analyse.extract_tags(text, topK=topk)
        return [w for w in re.findall(r'[\u4e00-\u9fff]{2,}', text) if len(w) >= 2]

    # -- 相似度 --

    @staticmethod
    def _normalize_title(title: str) -> str:
        """归一化标题用于去重比较（只保留中英文和数字）"""
        return re.sub(r'[^\u4e00-\u9fffa-zA-Z0-9]', '', title).lower()

    @staticmethod
    def _char_bigrams(text: str) -> set[str]:
        clean = re.sub(r'[^\u4e00-\u9fffa-zA-Z0-9]', '', text.lower())
        if len(clean) < 2:
            return {clean} if clean else set()
        return {clean[i:i+2] for i in range(len(clean) - 1)}

    def title_similarity(self, title_a: str, title_b: str) -> dict:
        """
        计算两个标题的综合相似度。

        返回详细数据供大模型参考：
        - keyword_jaccard: 关键词集合重叠率
        - bigram_jaccard:  字符 bigram 重叠率
        - combined:        加权混合值（0~1）
        - shared_keywords: 两个标题共有的关键词
        - unique_keywords_a/b: 各自独有的关键词
        """
        kw_a = set(self._extract_keywords(title_a, topk=8))
        kw_b = set(self._extract_keywords(title_b, topk=8))

        # 关键词 Jaccard
        if kw_a and kw_b:
            kw_jaccard = len(kw_a & kw_b) / len(kw_a | kw_b)
        else:
            kw_jaccard = 0.0

        # 字符 bigram Jaccard
        bg_a = self._char_bigrams(title_a)
        bg_b = self._char_bigrams(title_b)
        if bg_a and bg_b:
            bg_jaccard = len(bg_a & bg_b) / len(bg_a | bg_b)
        else:
            bg_jaccard = 0.0

        combined = 0.5 * kw_jaccard + 0.5 * bg_jaccard

        return {
            "combined": round(combined, 3),
            "keyword_jaccard": round(kw_jaccard, 3),
            "bigram_jaccard": round(bg_jaccard, 3),
            "shared_keywords": list(kw_a & kw_b),
            "unique_keywords_new": list(kw_a - kw_b),
            "unique_keywords_old": list(kw_b - kw_a),
        }

    # -- 历史扫描 --

    def scan_history(self, days: int = 7) -> list[PublishedArticle]:
        """
        扫描最近 N 天的已发布文章。

        优先读取 daily_report.json（结构化索引，速度快、信息全），
        找不到时降级到扫描目录 + 解析文件。
        """
        articles = []
        today = datetime.now()

        for i in range(days + 1):
            date = today - timedelta(days=i)
            date_str = date.strftime("%Y%m%d")
            date_dir = self._date_dir(date_str)
            if not date_dir.is_dir():
                continue

            # 优先读 daily_report.json
            report_file = date_dir / "daily_report.json"
            if report_file.is_file():
                day_articles = self._load_daily_report(report_file, date_str)
                if day_articles:
                    articles.extend(day_articles)
                    continue

            # 降级：扫目录
            link_file = date_dir / "文章链接.txt"
            if link_file.is_file():
                articles.extend(self._parse_link_file(link_file, date_str))

            existing_titles = {a.title for a in articles if a.date == date_str}
            for sub in sorted(date_dir.iterdir()):
                if sub.is_dir() and sub.name.startswith("article"):
                    html_file = sub / "content.html"
                    if html_file.is_file():
                        title = self._extract_title_from_html(html_file)
                        if title and title not in existing_titles:
                            articles.append(PublishedArticle(
                                title=title, date=date_str, article_dir=str(sub),
                            ))

        return articles

    def _load_daily_report(self, report_path: Path, date_str: str) -> list[PublishedArticle]:
        """从 daily_report.json 加载文章列表（含丰富元数据）"""
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            return []

        articles = []
        seen_titles = set()
        for art in data.get("articles", []):
            title = art.get("title", "")
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            articles.append(PublishedArticle(
                title=title,
                date=date_str,
                url=art.get("url", ""),
                article_dir=art.get("dir", ""),
                keywords=art.get("keywords", []),
                subheadings=art.get("subheadings", []),
                excerpt=art.get("excerpt", ""),
                char_count=art.get("char_count", 0),
                image_count=art.get("image_count", 0),
            ))
        return articles

    def _parse_link_file(self, path: Path, date_str: str) -> list[PublishedArticle]:
        articles = []
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return articles
        for line in text.strip().splitlines():
            m = re.match(r'^[\d\-\*\.]+\s*\[([^\]]+)\]\(([^)]+)\)', line.strip())
            if m:
                articles.append(PublishedArticle(
                    title=m.group(1), date=date_str, url=m.group(2),
                ))
        return articles

    def _extract_title_from_html(self, path: Path) -> Optional[str]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return None
        m = re.search(r'<h1[^>]*>(.*?)</h1>', text, re.DOTALL | re.IGNORECASE)
        if m:
            return re.sub(r'<[^>]+>', '', m.group(1)).strip()
        m = re.search(r'<title[^>]*>(.*?)</title>', text, re.DOTALL | re.IGNORECASE)
        if m:
            return re.sub(r'<[^>]+>', '', m.group(1)).strip()
        return None

    # -- 核心：为候选话题提供相似度数据 --

    def check_candidates(self, candidates: list[str], days: int = 3) -> dict:
        """
        对候选标题计算与所有历史文章的相似度。

        返回结构化数据，由大模型决定选题。
        每个候选标题会列出它与所有历史文章的相似度明细（按相似度降序）。
        如果历史文章来自 daily_report.json，会附带 keywords/subheadings/excerpt 等元数据。
        """
        history = self.scan_history(days=days)
        today_str = datetime.now().strftime("%Y%m%d")
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

        # 去重历史（归一化标题去重，优先保留有丰富元数据的）
        seen = {}
        for art in history:
            key = self._normalize_title(art.title)
            if key not in seen or art.keywords or art.url:
                seen[key] = art
        unique_history = list(seen.values())

        results = []
        for candidate in candidates:
            kw_new = self._extract_keywords(candidate, topk=8)
            comparisons = []

            for art in unique_history:
                sim = self.title_similarity(candidate, art.title)
                if sim["combined"] > 0.05:  # 只返回有一定相关度的
                    # 标记时间距离
                    if art.date == today_str:
                        time_distance = "today"
                    elif art.date == yesterday_str:
                        time_distance = "yesterday"
                    else:
                        time_distance = f"{art.date}"

                    entry = {
                        "history_title": art.title,
                        "history_url": art.url,
                        "time_distance": time_distance,
                        **sim,
                    }

                    # 如果有日报元数据，附上历史文章的内容摘要
                    if art.keywords:
                        entry["history_keywords"] = art.keywords
                    if art.subheadings:
                        entry["history_subheadings"] = art.subheadings
                    if art.excerpt:
                        entry["history_excerpt"] = art.excerpt
                    if art.char_count:
                        entry["history_char_count"] = art.char_count
                    if art.image_count:
                        entry["history_image_count"] = art.image_count

                    comparisons.append(entry)

            # 按相似度降序
            comparisons.sort(key=lambda x: x["combined"], reverse=True)

            results.append({
                "candidate": candidate,
                "keywords": kw_new,
                "comparisons": comparisons,
                "most_similar": comparisons[0] if comparisons else None,
            })

        return {
            "candidates": results,
            "history_summary": {
                "total_articles": len(unique_history),
                "dates": sorted({a.date for a in unique_history}, reverse=True),
                "today_count": sum(1 for a in unique_history if a.date == today_str),
                "yesterday_count": sum(1 for a in unique_history if a.date == yesterday_str),
                "with_daily_report": sum(1 for a in unique_history if a.keywords),
            },
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="热点话题数据层")
    parser.add_argument("--base-dir", default=r"D:\头条当日热点文章")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="候选标题 vs 历史相似度")
    p_check.add_argument("--candidates", required=True, help="逗号分隔")
    p_check.add_argument("--days", type=int, default=3)

    p_hist = sub.add_parser("history", help="查看发布历史")
    p_hist.add_argument("--days", type=int, default=7)

    args = parser.parse_args()
    mgr = HotTopicManager(base_dir=args.base_dir)

    if args.command == "check":
        candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
        print(json.dumps(mgr.check_candidates(candidates, days=args.days),
                         ensure_ascii=False, indent=2))

    elif args.command == "history":
        articles = mgr.scan_history(days=args.days)
        by_date = {}
        for a in articles:
            by_date.setdefault(a.date, []).append({"title": a.title, "url": a.url})
        print(json.dumps({
            "total": len(articles),
            "dates": {k: v for k, v in sorted(by_date.items(), reverse=True)},
        }, ensure_ascii=False, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
