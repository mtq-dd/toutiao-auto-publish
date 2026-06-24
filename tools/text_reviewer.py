#!/usr/bin/env python3
"""
文本审查模块：文章质量审查 + 内容合规审查

文章质量审查:
  - 字数统计与合理区间判断
  - 可读性分析（平均句长、段落长度分布）
  - 词汇多样性（TTR 去重比）
  - 高频重复词检测
  - 标题质量评分（长度、吸引力、信息量）
  - 结构完整性（开头/正文/结尾）

内容合规审查:
  - 敏感词分级匹配（政治、暴力、色情、违禁品）
  - 广告法违规词（绝对化用语、虚假承诺）
  - 医疗声明检测（疗效承诺、处方药推广）
  - 金融声明检测（收益承诺、保本保息）
  - 诱导内容检测（标题党、虚假紧迫感）
  - 引流行为检测（微信号、QQ号、外链引导）

依赖: pip install jieba
"""

import re
import os
import json
import logging
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

logger = logging.getLogger("text_reviewer")

# 尝试导入 jieba，失败时用简单分词
try:
    import jieba
    import jieba.analyse
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False
    logger.warning("jieba 未安装，将使用简单分词（准确度较低）。建议: pip install jieba")


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass
class ReviewIssue:
    severity: str     # "error" | "warning" | "info"
    category: str
    message: str
    context: str = ""       # 问题上下文（原文片段）
    position: int = -1      # 在原文中的位置
    suggestion: str = ""    # 修复建议

    def to_dict(self):
        d = asdict(self)
        if d["position"] == -1:
            d.pop("position")
        if not d["context"]:
            d.pop("context")
        if not d["suggestion"]:
            d.pop("suggestion")
        return d


@dataclass
class TextReviewReport:
    score: int = 100
    issues: list = field(default_factory=list)
    summary: str = ""
    stats: dict = field(default_factory=dict)

    def add(self, issue: ReviewIssue):
        self.issues.append(issue)
        if issue.severity == "error":
            self.score -= 12
        elif issue.severity == "warning":
            self.score -= 5
        else:
            self.score -= 1
        self.score = max(0, self.score)

    def to_dict(self):
        return {
            "score": self.score,
            "summary": self.summary,
            "stats": self.stats,
            "issues": [i.to_dict() for i in self.issues],
        }


# ===========================================================================
# 分词工具
# ===========================================================================
def _tokenize(text: str) -> list[str]:
    """中文分词，优先使用 jieba。"""
    if HAS_JIEBA:
        return list(jieba.cut(text))
    # 简单回退：按标点+空格分割
    return [w for w in re.split(r'[\s，。！？、；：""''（）《》【】\.\!\?\,\;\:]+', text) if w]


def _extract_keywords(text: str, topk: int = 20) -> list[tuple[str, float]]:
    """提取关键词。"""
    if HAS_JIEBA:
        return jieba.analyse.extract_tags(text, topK=topk, withWeight=True)
    # 简单回退：按词频
    words = [w for w in _tokenize(text) if len(w) >= 2]
    counter = Counter(words)
    return counter.most_common(topk)


# ===========================================================================
# 敏感词库
# ===========================================================================
class SensitiveWordDB:
    """
    敏感词库管理。

    支持从文件加载，也内置了一组常见违规词作为默认库。
    词库分为多个类别，每个类别有不同严重程度。
    """

    # 默认内置词库（实际使用时建议从文件加载更完整的词库）
    DEFAULT_WORDS = {
        # --- 严重违规 (error) ---
        "political": {
            "severity": "error",
            "words": [],  # 政治敏感词需要从外部文件加载，此处不内置
        },
        "violence": {
            "severity": "error",
            "words": [
                "恐怖袭击", "自杀方法", "制造炸弹", "制作毒品",
                "买枪", "卖枪", "管制刀具", "枪支弹药",
            ],
        },
        "pornography": {
            "severity": "error",
            "words": [
                "色情服务", "约炮", "上门服务", "小姐电话",
                "裸聊", "色情直播",
            ],
        },
        "contraband": {
            "severity": "error",
            "words": [
                "代购毒品", "冰毒", "海洛因", "大麻购买",
                "假币", "办假证", "伪造证件",
            ],
        },

        # --- 广告法违规 (warning/error) ---
        "ad_absolute": {
            "severity": "warning",
            "words": [
                "最好", "最佳", "第一", "唯一", "首个", "首选",
                "国家级", "世界级", "顶级", "极品", "绝对",
                "史上最", "全网最低", "独家", "万能", "100%",
                "永久", "无敌", "最先进", "最优", "最强",
                "最新技术", "最高级", "极致",
            ],
        },
        "ad_false_claim": {
            "severity": "error",
            "words": [
                "包治百病", "药到病除", "根治", "一次见效",
                "无副作用", "立竿见影", "祖传秘方",
                "特效药", "偏方治大病",
            ],
        },

        # --- 医疗健康声明 (warning) ---
        "medical_claim": {
            "severity": "warning",
            "words": [
                "保证治愈", "不复发", "彻底根治", "神奇疗效",
                "临床证明", "专家推荐", "医院同款",
                "替代药物", "停药", "不用手术",
            ],
        },

        # --- 金融声明 (warning) ---
        "financial_claim": {
            "severity": "warning",
            "words": [
                "保本保息", "稳赚不赔", "零风险", "日赚",
                "躺赚", "保证收益", "高额回报", "翻倍",
                "一夜暴富", "财务自由", "内幕消息",
            ],
        },

        # --- 诱导/标题党 (warning) ---
        "clickbait": {
            "severity": "warning",
            "words": [
                "不看后悔", "不转不是中国人", "赶紧看",
                "马上删除", "速看", "震惊", "太可怕了",
                "99%的人不知道", "千万别", "必看",
                "不看亏大了", "转发保平安",
            ],
        },

        # --- 引流行为 (warning) ---
        "traffic_diversion": {
            "severity": "warning",
            "words": [
                "加微信", "加我微信", "微信号", "wx号",
                "加QQ", "QQ群", "扫码加", "关注公众号",
                "私信领取", "评论区领取", "点击链接",
                "复制口令", "淘口令", "拼多多砍价",
            ],
        },

        # --- 低俗用语 (info) ---
        "vulgar": {
            "severity": "info",
            "words": [
                "牛逼", "傻逼", "卧槽", "他妈的", "尼玛",
                "靠北", "妈蛋",
            ],
        },
    }

    def __init__(self, extra_word_file: Optional[str] = None):
        self.categories: dict[str, dict] = {}
        self._load_defaults()
        if extra_word_file:
            self._load_from_file(extra_word_file)

    def _load_defaults(self):
        self.categories = self.DEFAULT_WORDS.copy()

    def _load_from_file(self, filepath: str):
        """
        从 JSON 文件加载词库。格式:
        {
            "category_name": {
                "severity": "error|warning|info",
                "words": ["词1", "词2", ...]
            }
        }
        """
        if not os.path.isfile(filepath):
            logger.warning(f"词库文件不存在: {filepath}")
            return

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        for cat, info in data.items():
            if cat in self.categories:
                # 合并
                existing = set(self.categories[cat]["words"])
                existing.update(info.get("words", []))
                self.categories[cat]["words"] = list(existing)
                if "severity" in info:
                    self.categories[cat]["severity"] = info["severity"]
            else:
                self.categories[cat] = info

        logger.info(f"已从 {filepath} 加载词库，共 {len(self.categories)} 个类别")

    def scan(self, text: str) -> list[dict]:
        """
        扫描文本中的敏感词。

        Returns:
            匹配结果列表，每项包含 category, severity, word, position, context
        """
        results = []
        text_lower = text.lower()

        for category, info in self.categories.items():
            words = info.get("words", [])
            severity = info.get("severity", "warning")

            for word in words:
                word_lower = word.lower()
                start = 0
                while True:
                    pos = text_lower.find(word_lower, start)
                    if pos == -1:
                        break
                    # 提取上下文
                    ctx_start = max(0, pos - 15)
                    ctx_end = min(len(text), pos + len(word) + 15)
                    context = text[ctx_start:ctx_end]

                    results.append({
                        "category": category,
                        "severity": severity,
                        "word": word,
                        "position": pos,
                        "context": f"...{context}...",
                    })
                    start = pos + 1

        return results


# ===========================================================================
# 文章质量审查
# ===========================================================================
class ArticleQualityReviewer:
    """
    文章质量审查器。

    评估维度: 字数、可读性、词汇多样性、重复词、标题质量、结构完整性。
    """

    # 可调参数
    CONFIG = {
        "min_word_count": 300,          # 最少字数
        "max_word_count": 10000,        # 最多字数
        "ideal_word_count": (500, 3000), # 理想字数区间
        "max_avg_sentence_len": 60,     # 平均句长上限
        "min_avg_sentence_len": 8,      # 平均句长下限
        "max_paragraph_len": 500,       # 单段最大字数
        "min_ttr": 0.3,                 # 最低词汇多样性（TTR）
        "repeated_word_threshold": 0.02, # 单词占比超过此值为高频重复
        "title_min_len": 5,
        "title_max_len": 30,
    }

    def review(self, title: str, content: str) -> TextReviewReport:
        report = TextReviewReport()

        # 纯文本（去 HTML 标签）
        plain = re.sub(r'<[^>]+>', '', content).strip()
        sentences = [s.strip() for s in re.split(r'[。！？\!\?\.]+', plain) if s.strip()]
        paragraphs = [p.strip() for p in re.split(r'\n\s*\n|\n', plain) if p.strip()]
        words = _tokenize(plain)
        words = [w.strip() for w in words if w.strip() and len(w.strip()) > 0]

        char_count = len(plain)
        word_count = len(words)
        sentence_count = len(sentences)
        paragraph_count = len(paragraphs)

        # 基础统计
        report.stats = {
            "字符数": char_count,
            "分词数": word_count,
            "句子数": sentence_count,
            "段落数": paragraph_count,
            "平均句长": round(char_count / max(sentence_count, 1), 1),
        }

        # -- 1. 字数检查 --
        cfg = self.CONFIG
        if char_count < cfg["min_word_count"]:
            report.add(ReviewIssue(
                severity="warning",
                category="字数",
                message=f"文章字数偏少（{char_count} 字），建议不少于 {cfg['min_word_count']} 字",
                suggestion="增加正文内容，头条文章推荐 500-3000 字",
            ))
        elif char_count > cfg["max_word_count"]:
            report.add(ReviewIssue(
                severity="info",
                category="字数",
                message=f"文章字数较多（{char_count} 字），建议考虑分段发布",
            ))

        # -- 2. 可读性 --
        if sentence_count > 0:
            avg_sentence = char_count / sentence_count
            report.stats["平均句长"] = round(avg_sentence, 1)

            if avg_sentence > cfg["max_avg_sentence_len"]:
                report.add(ReviewIssue(
                    severity="warning",
                    category="可读性",
                    message=f"平均句长过长（{avg_sentence:.0f} 字/句），影响阅读体验",
                    suggestion="将长句拆分为短句，每句建议不超过 40 字",
                ))
            elif avg_sentence < cfg["min_avg_sentence_len"]:
                report.add(ReviewIssue(
                    severity="info",
                    category="可读性",
                    message=f"平均句长偏短（{avg_sentence:.0f} 字/句），可能是碎片化表达",
                ))

        # 段落长度
        long_paragraphs = [(i, len(p)) for i, p in enumerate(paragraphs) if len(p) > cfg["max_paragraph_len"]]
        for idx, length in long_paragraphs:
            report.add(ReviewIssue(
                severity="info",
                category="可读性",
                message=f"第 {idx+1} 段过长（{length} 字），建议拆分",
                context=paragraphs[idx][:60] + "...",
                suggestion="每段建议 100-300 字，便于移动端阅读",
            ))

        # -- 3. 词汇多样性 --
        if word_count > 20:
            unique_words = set(words)
            ttr = len(unique_words) / word_count
            report.stats["词汇多样性(TTR)"] = round(ttr, 3)

            if ttr < cfg["min_ttr"]:
                report.add(ReviewIssue(
                    severity="warning",
                    category="词汇多样性",
                    message=f"词汇重复率较高（TTR={ttr:.2f}），文章用词单一",
                    suggestion="尝试使用同义词替换重复出现的词语",
                ))

        # -- 4. 高频重复词 --
        if word_count > 30:
            meaningful_words = [w for w in words if len(w) >= 2]
            counter = Counter(meaningful_words)
            for word, count in counter.most_common(10):
                ratio = count / max(len(meaningful_words), 1)
                if ratio > cfg["repeated_word_threshold"] and count > 5:
                    report.add(ReviewIssue(
                        severity="info",
                        category="重复词",
                        message=f"「{word}」出现 {count} 次（占比 {ratio*100:.1f}%），考虑替换",
                    ))

        # -- 5. 标题质量 --
        if title:
            title_len = len(title)
            report.stats["标题字数"] = title_len

            if title_len < cfg["title_min_len"]:
                report.add(ReviewIssue(
                    severity="error",
                    category="标题",
                    message=f"标题过短（{title_len} 字），平台要求 5-30 字",
                    context=title,
                    suggestion="标题需 5-30 字，建议 18-26 字并包含核心关键词",
                ))
            elif title_len > cfg["title_max_len"]:
                report.add(ReviewIssue(
                    severity="error",
                    category="标题",
                    message=f"标题超长（{title_len} 字），平台硬限制最多 30 字",
                    context=title[:50],
                    suggestion=f"当前 {title_len} 字，请删减至 30 字以内",
                ))

            # 标题是否有数字（有数字的标题通常点击率更高）
            has_number = bool(re.search(r'\d', title))
            report.stats["标题含数字"] = has_number

            # 标题是否有疑问句
            has_question = bool(re.search(r'[？?]', title))
            report.stats["标题含疑问"] = has_question
        else:
            report.add(ReviewIssue(
                severity="error",
                category="标题",
                message="标题为空",
            ))

        # -- 6. 结构完整性 --
        if paragraph_count < 2:
            report.add(ReviewIssue(
                severity="info",
                category="结构",
                message="文章段落过少，缺少起承转合",
            ))
        elif paragraph_count >= 3:
            # 简单检查：首段是否像引言，末段是否像总结
            first_para = paragraphs[0]
            last_para = paragraphs[-1]
            report.stats["首段字数"] = len(first_para)
            report.stats["末段字数"] = len(last_para)

        # 关键词
        if char_count > 100:
            keywords = _extract_keywords(plain, topk=10)
            report.stats["关键词"] = [(w, round(s, 3)) for w, s in keywords[:5]]

        # 摘要
        errors = sum(1 for i in report.issues if i.severity == "error")
        warnings = sum(1 for i in report.issues if i.severity == "warning")
        if errors == 0 and warnings == 0:
            report.summary = f"文章质量良好，评分 {report.score}/100"
        elif errors == 0:
            report.summary = f"文章质量基本合格，评分 {report.score}/100，有 {warnings} 个警告"
        else:
            report.summary = f"文章质量存在问题，评分 {report.score}/100，有 {errors} 个错误"

        return report


# ===========================================================================
# 内容合规审查
# ===========================================================================
class ContentComplianceReviewer:
    """
    内容合规审查器。

    使用关键词匹配 + 正则规则检测违规内容。
    """

    CATEGORY_LABELS = {
        "political": "政治敏感",
        "violence": "暴力内容",
        "pornography": "色情内容",
        "contraband": "违禁品",
        "ad_absolute": "广告法-绝对化用语",
        "ad_false_claim": "广告法-虚假宣传",
        "medical_claim": "医疗健康声明",
        "financial_claim": "金融收益承诺",
        "clickbait": "诱导/标题党",
        "traffic_diversion": "引流行为",
        "vulgar": "低俗用语",
    }

    SUGGESTIONS = {
        "political": "涉及政治敏感内容，头条平台严格审核，建议删除或修改",
        "violence": "涉及暴力内容，违反平台规范，建议删除",
        "pornography": "涉及色情内容，将被封禁，必须删除",
        "contraband": "涉及违禁品，违法内容，必须删除",
        "ad_absolute": "违反广告法，禁止使用绝对化用语，建议替换为客观描述",
        "ad_false_claim": "虚假宣传，违反广告法，可能面临处罚，必须修改",
        "medical_claim": "医疗声明需有资质支持，头条禁止未经验证的疗效宣传",
        "financial_claim": "金融收益承诺违反监管规定，头条禁止此类内容",
        "clickbait": "标题党/诱导内容会被平台降权，建议修改为客观标题",
        "traffic_diversion": "引流行为违反头条规范，可能被限流或封号",
        "vulgar": "低俗用语影响内容质量评级，建议使用文明用语",
    }

    # 正则模式（补充关键词之外的规则型检测）
    PATTERN_RULES = [
        {
            "pattern": r'(?:加|添|扫|关注).{0,5}(?:微信|vx|wx|WeChat)',
            "category": "traffic_diversion",
            "severity": "warning",
            "message": "疑似微信引流",
        },
        {
            "pattern": r'(?:QQ|扣扣|qq)\s*(?:群|号|:\s*\d)',
            "category": "traffic_diversion",
            "severity": "warning",
            "message": "疑似 QQ 引流",
        },
        {
            "pattern": r'\d{5,11}@(?:qq|163|126|gmail|yahoo)\.\w+',
            "category": "traffic_diversion",
            "severity": "info",
            "message": "包含邮箱地址（可能用于引流）",
        },
        {
            "pattern": r'(?:手机|电话|TEL|tel|☎)\s*[:：]?\s*1[3-9]\d{9}',
            "category": "traffic_diversion",
            "severity": "warning",
            "message": "包含手机号码",
        },
        {
            "pattern": r'(?:日赚|月入|日入)\s*\d+\s*(?:元|块|万)',
            "category": "financial_claim",
            "severity": "warning",
            "message": "收入承诺/金额诱导",
        },
        {
            "pattern": r'(?:治愈率|有效率|成功率)\s*(?:高达|达到)?\s*\d+\s*%',
            "category": "medical_claim",
            "severity": "error",
            "message": "具体疗效数据声明（需有医疗资质支持）",
        },
        {
            "pattern": r'(?:保证|承诺|确保)\s*(?:收益|回报|利润|赚)',
            "category": "financial_claim",
            "severity": "error",
            "message": "金融收益承诺",
        },
    ]

    def __init__(self, word_db: Optional[SensitiveWordDB] = None):
        self.db = word_db or SensitiveWordDB()

    def review(self, title: str, content: str) -> TextReviewReport:
        report = TextReviewReport()
        plain = re.sub(r'<[^>]+>', '', content).strip()
        full_text = title + " " + plain

        # -- 1. 关键词扫描 --
        matches = self.db.scan(full_text)

        # 去重（同一词在同一位置只报一次）
        seen = set()
        for m in matches:
            key = (m["word"], m["position"])
            if key in seen:
                continue
            seen.add(key)

            cat_label = self.CATEGORY_LABELS.get(m["category"], m["category"])
            report.add(ReviewIssue(
                severity=m["severity"],
                category=cat_label,
                message=f"检测到「{m['word']}」",
                context=m.get("context", ""),
                position=m.get("position", -1),
                suggestion=self.SUGGESTIONS.get(m["category"], ""),
            ))

        # -- 2. 正则规则检测 --
        for rule in self.PATTERN_RULES:
            for match in re.finditer(rule["pattern"], full_text):
                cat_label = self.CATEGORY_LABELS.get(rule["category"], rule["category"])
                ctx_start = max(0, match.start() - 10)
                ctx_end = min(len(full_text), match.end() + 10)
                report.add(ReviewIssue(
                    severity=rule["severity"],
                    category=cat_label,
                    message=rule["message"] + f"「{match.group()}」",
                    context=f"...{full_text[ctx_start:ctx_end]}...",
                    position=match.start(),
                    suggestion=self.SUGGESTIONS.get(rule["category"], ""),
                ))

        # -- 3. 统计 --
        report.stats = {
            "敏感词命中数": len(matches),
            "规则命中数": len(report.issues) - len(matches),
            "涉及类别": list(set(i.category for i in report.issues)),
        }

        # 摘要
        errors = sum(1 for i in report.issues if i.severity == "error")
        warnings = sum(1 for i in report.issues if i.severity == "warning")
        if errors == 0 and warnings == 0:
            report.summary = f"内容合规检查通过，评分 {report.score}/100"
        elif errors == 0:
            report.summary = f"发现 {warnings} 个警告，评分 {report.score}/100，建议修改后发布"
        else:
            report.summary = f"发现 {errors} 个严重违规，评分 {report.score}/100，必须修改"

        return report


# ===========================================================================
# 统一入口
# ===========================================================================
def review_text(
    title: str,
    content: str,
    word_db_file: Optional[str] = None,
) -> dict:
    """
    执行文章质量 + 内容合规双重审查。

    Args:
        title: 文章标题
        content: 文章正文（支持 HTML，会自动去标签）
        word_db_file: 自定义敏感词库 JSON 文件路径

    Returns:
        {
            "article_quality": { score, summary, stats, issues },
            "content_compliance": { score, summary, stats, issues },
            "overall_score": int,
            "overall_summary": str,
            "passed": bool,
        }
    """
    # 文章质量
    quality_reviewer = ArticleQualityReviewer()
    quality_report = quality_reviewer.review(title, content)

    # 内容合规
    db = SensitiveWordDB(extra_word_file=word_db_file)
    compliance_reviewer = ContentComplianceReviewer(word_db=db)
    compliance_report = compliance_reviewer.review(title, content)

    # 综合
    overall_score = min(quality_report.score, compliance_report.score)
    has_errors = any(i.severity == "error" for i in compliance_report.issues)
    passed = not has_errors and overall_score >= 60

    if passed:
        overall_summary = f"审查通过，综合评分 {overall_score}/100"
    elif has_errors:
        overall_summary = f"审查未通过（存在严重违规），综合评分 {overall_score}/100"
    else:
        overall_summary = f"审查未通过（评分过低），综合评分 {overall_score}/100"

    return {
        "article_quality": quality_report.to_dict(),
        "content_compliance": compliance_report.to_dict(),
        "overall_score": overall_score,
        "overall_summary": overall_summary,
        "passed": passed,
    }


# ===========================================================================
# CLI
# ===========================================================================
def _format_report(report_dict: dict, label: str) -> str:
    lines = [f"\n{'='*55}", f"  {label}", f"{'='*55}"]
    lines.append(f"  评分: {report_dict['score']}/100")
    lines.append(f"  摘要: {report_dict['summary']}")

    if report_dict.get("stats"):
        lines.append(f"  统计: {json.dumps(report_dict['stats'], ensure_ascii=False)}")

    if report_dict["issues"]:
        for severity, label_text in [("error", "错误"), ("warning", "警告"), ("info", "建议")]:
            issues = [i for i in report_dict["issues"] if i["severity"] == severity]
            if not issues:
                continue
            lines.append(f"\n  [{label_text}]")
            for idx, i in enumerate(issues, 1):
                lines.append(f"    {idx}. [{i['category']}] {i['message']}")
                if i.get("context"):
                    lines.append(f"       上下文: {i['context']}")
                if i.get("suggestion"):
                    lines.append(f"       建议: {i['suggestion']}")
    else:
        lines.append("  没有发现问题")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="文章质量 & 内容合规审查工具")
    parser.add_argument("--title", required=True, help="文章标题")
    parser.add_argument("--content", required=True, help="正文内容（文本或文件路径）")
    parser.add_argument("--word-db", help="自定义敏感词库 JSON 文件")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--output", "-o", help="结果写入文件")
    args = parser.parse_args()

    content = args.content
    if os.path.isfile(content):
        with open(content, "r", encoding="utf-8") as f:
            content = f.read()

    result = review_text(args.title, content, word_db_file=args.word_db)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_format_report(result["article_quality"], "文章质量审查"))
        print(_format_report(result["content_compliance"], "内容合规审查"))
        print(f"\n{'='*55}")
        print(f"  综合结果: {result['overall_summary']}")
        print(f"  {'通过' if result['passed'] else '未通过'}")
        print(f"{'='*55}\n")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    import sys
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
