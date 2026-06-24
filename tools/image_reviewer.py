#!/usr/bin/env python3
"""
图片审查模块：图片质量审查 + 图片合规审查

图片质量审查:
  - 分辨率检查（最小尺寸、推荐尺寸）
  - 模糊度检测（拉普拉斯方差 / 边缘梯度法）
  - 亮度检查（过暗/过曝）
  - 对比度检查
  - 宽高比合理性
  - 文件大小检查

图片合规审查:
  - 肤色比例检测（NSFW 初筛）
  - 图片内敏感文字检测（需 OCR 支持）
  - EXIF 元数据检查
  - 色域分布异常检测

依赖: pip install Pillow numpy
可选: pip install opencv-python  (更精准的模糊检测)
可选: pip install pytesseract    (图片文字识别)
"""

import os
import json
import math
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path

logger = logging.getLogger("image_reviewer")

try:
    from PIL import Image
    from PIL.ExifTags import TAGS as EXIF_TAGS
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    logger.error("Pillow 未安装，请运行: pip install Pillow")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    logger.warning("numpy 未安装，部分功能受限。建议: pip install numpy")

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
    logger.info("OpenCV 未安装，将使用 Pillow 替代方案（模糊检测精度略低）")

try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False
    logger.info("pytesseract 未安装，图片内文字检测不可用")


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass
class ImageIssue:
    severity: str     # "error" | "warning" | "info"
    category: str
    message: str
    image_path: str = ""
    detail: dict = field(default_factory=dict)
    suggestion: str = ""

    def to_dict(self):
        d = asdict(self)
        return {k: v for k, v in d.items() if v}


@dataclass
class ImageReviewReport:
    score: int = 100
    issues: list = field(default_factory=list)
    summary: str = ""
    image_count: int = 0
    per_image: dict = field(default_factory=dict)  # path -> {score, issues}

    def add(self, issue: ImageIssue):
        self.issues.append(issue)
        if issue.severity == "error":
            self.score -= 15
        elif issue.severity == "warning":
            self.score -= 6
        else:
            self.score -= 1
        self.score = max(0, self.score)

    def to_dict(self):
        return {
            "score": self.score,
            "summary": self.summary,
            "image_count": self.image_count,
            "issues": [i.to_dict() for i in self.issues],
            "per_image": self.per_image,
        }


# ===========================================================================
# 图片质量审查
# ===========================================================================
class ImageQualityReviewer:
    """
    图片质量审查器。

    支持本地文件路径和 PIL Image 对象。
    """

    CONFIG = {
        # 分辨率
        "min_width": 400,
        "min_height": 300,
        "recommended_width": 800,
        "recommended_height": 600,
        "max_width": 8000,
        "max_height": 8000,

        # 模糊度（拉普拉斯方差，越低越模糊）
        "blur_threshold": 80.0,       # 低于此值判定模糊
        "blur_warning_threshold": 150.0,  # 低于此值给出警告

        # 亮度（0-255 的平均值）
        "min_brightness": 40,         # 过暗
        "max_brightness": 235,        # 过曝

        # 对比度（标准差）
        "min_contrast": 20,           # 对比度过低（灰蒙蒙）
        "max_contrast": 120,          # 对比度过高

        # 宽高比
        "acceptable_ratios": [        # 可接受的宽高比（带容差）
            (1.0, 0.1),    # 1:1
            (1.333, 0.1),  # 4:3
            (1.5, 0.1),    # 3:2
            (1.778, 0.1),  # 16:9
            (0.75, 0.1),   # 3:4
            (0.667, 0.1),  # 2:3
            (0.5625, 0.1), # 9:16
        ],

        # 文件大小
        "max_file_size_mb": 20,
        "min_file_size_kb": 5,

        # 格式
        "allowed_formats": ["JPEG", "PNG", "WEBP", "GIF"],
    }

    def review_file(self, file_path: str) -> dict:
        """审查单张图片文件，返回审查详情。"""
        if not HAS_PIL:
            return {"error": "Pillow 未安装"}

        if not os.path.isfile(file_path):
            return {"error": f"文件不存在: {file_path}"}

        cfg = self.CONFIG
        issues = []
        details = {"path": file_path}

        try:
            img = Image.open(file_path)
        except Exception as e:
            issues.append(ImageIssue(
                severity="error", category="文件",
                message=f"无法打开图片: {e}",
                image_path=file_path,
            ))
            return {"issues": issues, "details": details}

        w, h = img.size
        fmt = img.format or "unknown"
        file_size = os.path.getsize(file_path)

        details["width"] = w
        details["height"] = h
        details["format"] = fmt
        details["file_size_kb"] = round(file_size / 1024, 1)
        details["mode"] = img.mode

        # -- 1. 分辨率 --
        if w < cfg["min_width"] or h < cfg["min_height"]:
            issues.append(ImageIssue(
                severity="warning", category="分辨率",
                message=f"图片分辨率过小 ({w}x{h})，建议不小于 {cfg['min_width']}x{cfg['min_height']}",
                image_path=file_path,
                suggestion="头条推荐文章配图宽度不低于 800px",
            ))
        elif w < cfg["recommended_width"] or h < cfg["recommended_height"]:
            issues.append(ImageIssue(
                severity="info", category="分辨率",
                message=f"图片分辨率偏低 ({w}x{h})，推荐 {cfg['recommended_width']}x{cfg['recommended_height']} 以上",
                image_path=file_path,
            ))

        if w > cfg["max_width"] or h > cfg["max_height"]:
            issues.append(ImageIssue(
                severity="warning", category="分辨率",
                message=f"图片分辨率过大 ({w}x{h})，可能导致加载缓慢",
                image_path=file_path,
                suggestion="建议缩小到 2000px 以内",
            ))

        # -- 2. 格式 --
        if fmt not in cfg["allowed_formats"]:
            issues.append(ImageIssue(
                severity="warning", category="格式",
                message=f"图片格式 {fmt} 不在推荐列表中",
                image_path=file_path,
                suggestion=f"推荐使用: {', '.join(cfg['allowed_formats'])}",
            ))

        # -- 3. 文件大小 --
        size_mb = file_size / (1024 * 1024)
        if size_mb > cfg["max_file_size_mb"]:
            issues.append(ImageIssue(
                severity="warning", category="文件大小",
                message=f"图片文件过大 ({size_mb:.1f}MB)",
                image_path=file_path,
                suggestion="建议压缩到 5MB 以内，可使用 TinyPNG 等工具",
            ))

        size_kb = file_size / 1024
        if size_kb < cfg["min_file_size_kb"]:
            issues.append(ImageIssue(
                severity="info", category="文件大小",
                message=f"图片文件过小 ({size_kb:.1f}KB)，可能是占位图或图标",
                image_path=file_path,
            ))

        # -- 4. 模糊度 --
        blur_score = self._calc_blur_score(img)
        if blur_score is not None:
            details["blur_score"] = round(blur_score, 1)
            if blur_score < cfg["blur_threshold"]:
                issues.append(ImageIssue(
                    severity="error", category="模糊度",
                    message=f"图片明显模糊 (清晰度={blur_score:.0f}，阈值={cfg['blur_threshold']:.0f})",
                    image_path=file_path,
                    detail={"blurScore": round(blur_score, 1)},
                    suggestion="使用更高分辨率的原图，或重新拍摄/导出",
                ))
            elif blur_score < cfg["blur_warning_threshold"]:
                issues.append(ImageIssue(
                    severity="info", category="模糊度",
                    message=f"图片清晰度一般 (清晰度={blur_score:.0f})",
                    image_path=file_path,
                    detail={"blurScore": round(blur_score, 1)},
                ))

        # -- 5. 亮度 --
        brightness, contrast = self._calc_brightness_contrast(img)
        if brightness is not None:
            details["brightness"] = round(brightness, 1)
            details["contrast"] = round(contrast, 1)

            if brightness < cfg["min_brightness"]:
                issues.append(ImageIssue(
                    severity="warning", category="亮度",
                    message=f"图片过暗 (亮度={brightness:.0f}，最低={cfg['min_brightness']})",
                    image_path=file_path,
                    suggestion="调整亮度或使用补光拍摄",
                ))
            elif brightness > cfg["max_brightness"]:
                issues.append(ImageIssue(
                    severity="warning", category="亮度",
                    message=f"图片过曝 (亮度={brightness:.0f}，最高={cfg['max_brightness']})",
                    image_path=file_path,
                    suggestion="降低曝光度或调整后期",
                ))

            if contrast < cfg["min_contrast"]:
                issues.append(ImageIssue(
                    severity="warning", category="对比度",
                    message=f"图片对比度过低 (对比度={contrast:.0f})，画面灰蒙蒙",
                    image_path=file_path,
                    suggestion="增加对比度让画面更鲜明",
                ))
            elif contrast > cfg["max_contrast"]:
                issues.append(ImageIssue(
                    severity="info", category="对比度",
                    message=f"图片对比度较高 (对比度={contrast:.0f})，可能失真",
                    image_path=file_path,
                ))

        # -- 6. 宽高比 --
        ratio = w / h if h > 0 else 0
        details["aspect_ratio"] = round(ratio, 3)

        ratio_ok = False
        for target_r, tolerance in cfg["acceptable_ratios"]:
            if abs(ratio - target_r) <= tolerance:
                ratio_ok = True
                break

        if not ratio_ok and ratio > 0:
            issues.append(ImageIssue(
                severity="info", category="宽高比",
                message=f"图片宽高比 {ratio:.2f} 不是常见比例",
                image_path=file_path,
                detail={"ratio": round(ratio, 3)},
                suggestion="推荐使用 16:9, 4:3, 3:2, 1:1 等常见比例",
            ))

        # 单图评分
        img_score = 100
        for i in issues:
            if i.severity == "error":
                img_score -= 15
            elif i.severity == "warning":
                img_score -= 6
            else:
                img_score -= 1
        img_score = max(0, img_score)
        details["score"] = img_score

        return {"issues": issues, "details": details}

    def _calc_blur_score(self, img: Image.Image) -> Optional[float]:
        """
        计算图片清晰度（越高越清晰）。

        优先用 OpenCV（拉普拉斯方差），回退到 Pillow 边缘梯度法。
        """
        if HAS_CV2:
            try:
                gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
                return float(cv2.Laplacian(gray, cv2.CV_64F).var())
            except Exception:
                pass

        if HAS_NUMPY and HAS_PIL:
            try:
                # Pillow 版边缘检测（Sobel 近似）
                gray = img.convert("L")
                arr = np.array(gray, dtype=np.float64)

                # 水平梯度
                gx = np.diff(arr, axis=1)
                # 垂直梯度
                gy = np.diff(arr, axis=0)

                # 梯度幅度的方差
                gx_var = np.var(gx)
                gy_var = np.var(gy)
                return float((gx_var + gy_var) / 2)
            except Exception:
                pass

        return None

    def _calc_brightness_contrast(self, img: Image.Image) -> tuple[Optional[float], Optional[float]]:
        """计算亮度和对比度（标准差）。"""
        if not HAS_NUMPY:
            return None, None

        try:
            gray = np.array(img.convert("L"), dtype=np.float64)
            brightness = float(np.mean(gray))
            contrast = float(np.std(gray))
            return brightness, contrast
        except Exception:
            return None, None


# ===========================================================================
# 图片合规审查
# ===========================================================================
class ImageComplianceReviewer:
    """
    图片合规审查器。

    检测维度：
    - 肤色比例（NSFW 初筛）
    - 图片内敏感文字（OCR）
    - EXIF 异常
    - 色域分布异常
    """

    CONFIG = {
        # 肤色比例阈值
        "skin_ratio_warning": 0.55,   # 超过 55% 给警告
        "skin_ratio_error": 0.75,     # 超过 75% 给错误

        # 敏感文字（与 text_reviewer 共享词库，这里内置精简版）
        "sensitive_words_in_image": [
            "加微信", "微信号", "加我", "扫码",
            "QQ群", "淘口令", "复制口令",
            "免费领", "日赚", "月入",
            "包治", "根治", "特效",
        ],
    }

    def review_file(self, file_path: str) -> dict:
        """审查单张图片的合规性。"""
        if not HAS_PIL or not os.path.isfile(file_path):
            return {"issues": []}

        issues = []
        details = {"path": file_path}

        try:
            img = Image.open(file_path)
        except Exception as e:
            issues.append(ImageIssue(
                severity="error", category="文件",
                message=f"无法打开图片: {e}",
                image_path=file_path,
            ))
            return {"issues": issues, "details": details}

        # -- 1. 肤色比例检测 --
        skin_ratio = self._calc_skin_ratio(img)
        if skin_ratio is not None:
            details["skin_ratio"] = round(skin_ratio, 3)

            if skin_ratio > self.CONFIG["skin_ratio_error"]:
                issues.append(ImageIssue(
                    severity="error", category="敏感内容",
                    message=f"图片肤色比例过高 ({skin_ratio*100:.0f}%)，可能包含不当内容",
                    image_path=file_path,
                    detail={"skinRatio": round(skin_ratio, 3)},
                    suggestion="请更换合规图片，此类内容会被平台审核拦截",
                ))
            elif skin_ratio > self.CONFIG["skin_ratio_warning"]:
                issues.append(ImageIssue(
                    severity="warning", category="敏感内容",
                    message=f"图片肤色比例偏高 ({skin_ratio*100:.0f}%)，可能触发平台审核",
                    image_path=file_path,
                    detail={"skinRatio": round(skin_ratio, 3)},
                    suggestion="建议更换图片，或在发布前人工确认内容合规",
                ))

        # -- 2. 图片内文字检测（OCR） --
        if HAS_OCR:
            text_in_image = self._extract_text(img)
            if text_in_image:
                details["ocr_text"] = text_in_image[:200]
                sensitive_found = self._check_text_sensitive(text_in_image)
                for word in sensitive_found:
                    issues.append(ImageIssue(
                        severity="warning", category="图片文字",
                        message=f"图片中包含敏感文字「{word}」",
                        image_path=file_path,
                        detail={"ocrText": text_in_image[:100]},
                        suggestion="图片中的文字也会被平台审核检查",
                    ))

        # -- 3. EXIF 检查 --
        exif_issues = self._check_exif(img, file_path)
        issues.extend(exif_issues)

        # -- 4. 色域分布异常 --
        color_issue = self._check_color_distribution(img)
        if color_issue:
            issues.append(color_issue)

        # 单图评分
        img_score = 100
        for i in issues:
            if i.severity == "error":
                img_score -= 15
            elif i.severity == "warning":
                img_score -= 6
            else:
                img_score -= 1
        img_score = max(0, img_score)
        details["score"] = img_score

        return {"issues": issues, "details": details}

    def _calc_skin_ratio(self, img: Image.Image) -> Optional[float]:
        """
        计算图片中的肤色像素比例。

        使用 HSV 色彩空间的肤色范围检测。
        这是一个简单的启发式方法，不是精确的 NSFW 分类器。
        """
        if not HAS_NUMPY:
            return None

        try:
            # 转 HSV
            rgb = img.convert("RGB")
            arr = np.array(rgb, dtype=np.float64)
            r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]

            # 肤色判据（多个条件取交集）
            # 条件1: RGB 规则
            rule1 = (r > 95) & (g > 40) & (b > 20)
            rule2 = (r > g) & (r > b)
            rule3 = (np.maximum(r, np.maximum(g, b)) - np.minimum(r, np.minimum(g, b)) > 15)
            rule4 = (np.abs(r - g) > 15)

            skin_mask = rule1 & rule2 & rule3 & rule4

            total_pixels = arr.shape[0] * arr.shape[1]
            skin_pixels = int(np.sum(skin_mask))

            return skin_pixels / total_pixels if total_pixels > 0 else 0
        except Exception:
            return None

    def _extract_text(self, img: Image.Image) -> Optional[str]:
        """用 Tesseract OCR 提取图片中的文字。"""
        if not HAS_OCR:
            return None
        try:
            # 中文 + 英文
            text = pytesseract.image_to_string(img, lang="chi_sim+eng")
            return text.strip() if text.strip() else None
        except Exception as e:
            logger.debug(f"OCR 失败: {e}")
            return None

    def _check_text_sensitive(self, text: str) -> list[str]:
        """检查图片中文字是否包含敏感词。"""
        found = []
        text_lower = text.lower()
        for word in self.CONFIG["sensitive_words_in_image"]:
            if word.lower() in text_lower:
                found.append(word)
        return found

    def _check_exif(self, img: Image.Image, file_path: str) -> list[ImageIssue]:
        """检查 EXIF 元数据。"""
        issues = []
        try:
            exif_data = img._getexif()
            if exif_data:
                info = {}
                for tag_id, value in exif_data.items():
                    tag = EXIF_TAGS.get(tag_id, str(tag_id))
                    info[tag] = str(value)[:100]

                # 检查 GPS 信息（隐私风险）
                if "GPSInfo" in exif_data:
                    issues.append(ImageIssue(
                        severity="warning", category="隐私",
                        message="图片包含 GPS 位置信息（隐私风险）",
                        image_path=file_path,
                        suggestion="建议去除 EXIF 中的 GPS 数据后再上传",
                    ))

                # 检查设备信息
                if "Make" in info or "Model" in info:
                    logger.debug(f"拍摄设备: {info.get('Make', '')} {info.get('Model', '')}")
        except Exception:
            pass  # 没有 EXIF 数据是正常的

        return issues

    def _check_color_distribution(self, img: Image.Image) -> Optional[ImageIssue]:
        """
        检查色域分布异常。

        纯色图、极度偏色图等可能是低质量或异常图片。
        """
        if not HAS_NUMPY:
            return None

        try:
            arr = np.array(img.convert("RGB"), dtype=np.float64)

            # 各通道标准差
            std_r = np.std(arr[:, :, 0])
            std_g = np.std(arr[:, :, 1])
            std_b = np.std(arr[:, :, 2])

            # 几乎纯色图（所有通道标准差都很低）
            if std_r < 5 and std_g < 5 and std_b < 5:
                return ImageIssue(
                    severity="warning", category="色域异常",
                    message="图片几乎是纯色（可能是占位图或空白图）",
                    image_path="",
                    detail={"stdR": round(std_r, 1), "stdG": round(std_g, 1), "stdB": round(std_b, 1)},
                    suggestion="请更换为有实际内容的图片",
                )

            # 严重偏色
            means = [np.mean(arr[:, :, 0]), np.mean(arr[:, :, 1]), np.mean(arr[:, :, 2])]
            max_diff = max(means) - min(means)
            if max_diff > 120:
                return ImageIssue(
                    severity="info", category="色域异常",
                    message=f"图片严重偏色（RGB均值差异={max_diff:.0f}）",
                    image_path="",
                    detail={
                        "meanR": round(means[0], 1),
                        "meanG": round(means[1], 1),
                        "meanB": round(means[2], 1),
                    },
                )
        except Exception:
            pass

        return None


# ===========================================================================
# 统一入口
# ===========================================================================
def review_images(image_paths: list[str]) -> dict:
    """
    对多张图片执行质量 + 合规双重审查。

    Args:
        image_paths: 图片文件路径列表

    Returns:
        {
            "image_quality": { score, summary, issues, per_image },
            "image_compliance": { score, summary, issues, per_image },
            "overall_score": int,
            "overall_summary": str,
            "passed": bool,
        }
    """
    quality_reviewer = ImageQualityReviewer()
    compliance_reviewer = ImageComplianceReviewer()

    quality_report = ImageReviewReport(image_count=len(image_paths))
    compliance_report = ImageReviewReport(image_count=len(image_paths))

    for path in image_paths:
        # 质量审查
        q_result = quality_reviewer.review_file(path)
        for issue in q_result.get("issues", []):
            quality_report.add(issue)
        quality_report.per_image[os.path.basename(path)] = q_result.get("details", {})

        # 合规审查
        c_result = compliance_reviewer.review_file(path)
        for issue in c_result.get("issues", []):
            issue.image_path = path
            compliance_report.add(issue)
        compliance_report.per_image[os.path.basename(path)] = c_result.get("details", {})

    # 摘要
    def make_summary(report, label):
        errors = sum(1 for i in report.issues if i.severity == "error")
        warnings = sum(1 for i in report.issues if i.severity == "warning")
        if errors == 0 and warnings == 0:
            report.summary = f"{label}通过，评分 {report.score}/100"
        elif errors == 0:
            report.summary = f"{label}发现 {warnings} 个警告，评分 {report.score}/100"
        else:
            report.summary = f"{label}发现 {errors} 个错误，评分 {report.score}/100"

    make_summary(quality_report, "图片质量")
    make_summary(compliance_report, "图片合规")

    overall = min(quality_report.score, compliance_report.score)
    has_errors = any(i.severity == "error" for i in compliance_report.issues)
    passed = not has_errors and overall >= 50

    return {
        "image_quality": quality_report.to_dict(),
        "image_compliance": compliance_report.to_dict(),
        "overall_score": overall,
        "overall_summary": f"{'通过' if passed else '未通过'}，综合评分 {overall}/100",
        "passed": passed,
    }


# ===========================================================================
# CLI
# ===========================================================================
def _format_report(report_dict: dict, label: str) -> str:
    lines = [f"\n{'='*55}", f"  {label}", f"{'='*55}"]
    lines.append(f"  评分: {report_dict['score']}/100")
    lines.append(f"  摘要: {report_dict['summary']}")
    lines.append(f"  图片数: {report_dict.get('image_count', 'N/A')}")

    if report_dict["issues"]:
        for severity, sl in [("error", "错误"), ("warning", "警告"), ("info", "建议")]:
            issues = [i for i in report_dict["issues"] if i["severity"] == severity]
            if not issues:
                continue
            lines.append(f"\n  [{sl}]")
            for idx, i in enumerate(issues, 1):
                img_tag = f"[{os.path.basename(i.get('image_path', ''))}]" if i.get("image_path") else ""
                lines.append(f"    {idx}. [{i['category']}] {i['message']} {img_tag}")
                if i.get("suggestion"):
                    lines.append(f"       建议: {i['suggestion']}")
                if i.get("detail"):
                    d = json.dumps(i["detail"], ensure_ascii=False)
                    if len(d) > 120:
                        d = d[:120] + "..."
                    lines.append(f"       详情: {d}")
    else:
        lines.append("  没有发现问题")

    # 每张图的分数
    per_image = report_dict.get("per_image", {})
    if per_image:
        lines.append("\n  各图片评分:")
        for name, info in per_image.items():
            s = info.get("score", "N/A")
            lines.append(f"    - {name}: {s}/100")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="图片质量 & 合规审查工具")
    parser.add_argument("images", nargs="+", help="图片文件路径")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--output", "-o", help="结果写入文件")
    args = parser.parse_args()

    result = review_images(args.images)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(_format_report(result["image_quality"], "图片质量审查"))
        print(_format_report(result["image_compliance"], "图片合规审查"))
        print(f"\n{'='*55}")
        print(f"  综合: {result['overall_summary']}")
        print(f"{'='*55}\n")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    import sys
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
