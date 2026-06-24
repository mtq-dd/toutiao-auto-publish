#!/usr/bin/env python3
"""
头条全自动发布 - 最终版（已验证有效）

完整流程：
  1. 填写标题（native setter + React 兼容）
  2. 插入正文 HTML（execCommand + innerHTML 备选）
  3. 上传 3-4 张图片（ClipboardEvent 粘贴 → 头条自动上传到 CDN）
  4. 设置封面模式（三图，编辑器自动从正文选图）
  5. 发布（预览并发布 → 确认发布）

依赖: pip install websockets Pillow

用法:
  python auto_publish_final.py
  python auto_publish_final.py --title "自定义标题" --content path/to/content.html --images dir/
"""
import asyncio
import json
import os
import sys
import argparse
import base64
import time
from pathlib import Path

# 修复 Windows 编码
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, 'D:\\头条当日热点文章\\tools')
from toutiao_publisher import CDPClient, ToutiaoPublisher


async def create_test_images(img_dir: str, count: int = 4) -> list[str]:
    """创建测试图片（如果不存在）。"""
    os.makedirs(img_dir, exist_ok=True)
    images = []
    
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("需要 Pillow: pip install Pillow")
        return []
    
    colors = [(70, 130, 180), (60, 179, 113), (255, 165, 0), (180, 70, 130)]
    labels = ['SpaceX IPO 2026', 'Blue Origin', 'Space Race', 'Future Tech']
    
    for i in range(min(count, 4)):
        path = os.path.join(img_dir, f'img{i+1}.png')
        if not os.path.exists(path):
            img = Image.new('RGB', (1200, 675), colors[i % len(colors)])
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("arial.ttf", 60)
            except:
                font = ImageFont.load_default()
            bbox = draw.textbbox((0, 0), labels[i % len(labels)], font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((1200-tw)//2, (675-th)//2), labels[i % len(labels)], fill='white', font=font)
            img.save(path)
            print(f"  创建测试图片: {path}")
        images.append(path)
    
    return images


async def main():
    parser = argparse.ArgumentParser(description="头条全自动发布")
    parser.add_argument("--title", default="SpaceX计划IPO与蓝色起源火箭爆炸：2026太空竞赛新格局",
                       help="文章标题")
    parser.add_argument("--content", default="D:\\头条当日热点文章\\articles_published\\2026\\06\\12\\article1\\content.html",
                       help="正文 HTML 文件路径")
    parser.add_argument("--images", default="D:\\头条当日热点文章\\articles_published\\2026\\06\\12\\article1\\images",
                       help="图片目录路径")
    parser.add_argument("--port", type=int, default=9222, help="CDP 端口")
    parser.add_argument("--cover-mode", default="三图", choices=["单图", "三图", "无封面"],
                       help="封面模式")
    parser.add_argument("--dry-run", action="store_true", help="试运行（不发布）")
    args = parser.parse_args()
    
    print("=" * 60)
    print("  头条全自动发布 - 最终版")
    print("=" * 60)
    
    # 读取正文
    content_path = args.content
    if os.path.isfile(content_path):
        with open(content_path, 'r', encoding='utf-8') as f:
            content_html = f.read()
        print(f"\n正文: {content_path} ({len(content_html)} 字符)")
    else:
        print(f"正文文件不存在: {content_path}")
        return
    
    # 准备图片
    img_dir = args.images
    if os.path.isdir(img_dir):
        image_files = sorted([
            os.path.join(img_dir, f) 
            for f in os.listdir(img_dir) 
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp'))
        ])
    else:
        print(f"图片目录不存在: {img_dir}，创建测试图片...")
        image_files = await create_test_images(img_dir)
    
    print(f"标题: {args.title}")
    print(f"图片: {len(image_files)} 张")
    print(f"封面: {args.cover_mode}")
    print(f"模式: {'试运行' if args.dry_run else '正式发布'}")
    
    # 使用 ToutiaoPublisher 执行完整流程
    async with ToutiaoPublisher(port=args.port) as pub:
        result = await pub.publish_article(
            title=args.title,
            content=content_html,
            content_image_paths=image_files,
            is_html=True,
            auto_publish=not args.dry_run,
            cover_mode=args.cover_mode,
            dry_run=args.dry_run,
        )
    
    # 输出结果
    print(f"\n{'='*60}")
    print(f"  发布结果: {'成功' if result.get('published') else '未完成'}")
    print(f"{'='*60}")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
