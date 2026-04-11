#!/usr/bin/env python3
"""Generate featured images for Hugo blog posts."""

import os
from PIL import Image, ImageDraw, ImageFont
import math

# Post configs: (directory, title, icon_text, color_scheme)
POSTS = [
    ("docker-best-practices",    "Docker",           "🐳", [(15,40,70),  (0,100,160)]),
    ("gitops-argocd",            "GitOps",           "⚙",  [(20,50,30),  (30,130,80)]),
    ("prometheus-grafana",       "Observability",    "📊", [(60,20,60),  (150,50,140)]),
    ("kafka-ops-practice",       "Kafka",            "⚡",  [(60,30,10),  (200,100,0)]),
    ("linux-performance-tuning", "Linux Perf",       "🖥",  [(15,15,40),  (50,50,120)]),
    ("python-devops-automation", "Python DevOps",    "🐍", [(10,40,20),  (20,130,60)]),
    ("aws-eks-best-practices",   "AWS EKS",          "☁",  [(10,30,50),  (20,80,160)]),
    ("cicd-pipeline-design",     "CI/CD",            "🚀", [(40,10,40),  (120,30,120)]),
    ("karpenter-deep-dive",      "Karpenter",        "⚖",  [(40,20,10),  (150,70,10)]),
    ("database-ops-practice",    "Database",         "🗄",  [(10,30,40),  (10,90,130)]),
    # Existing posts
    ("SRE实践心得",              "SRE",              "🔧", [(30,10,40),  (100,30,140)]),
    ("k8s-成本优化实战",         "K8s FinOps",       "💰", [(20,40,10),  (60,140,30)]),
    ("云原生转型经验",           "Cloud Native",     "☸",  [(10,30,50),  (20,80,160)]),
    ("告警体系设计",             "Alerting",         "🔔", [(50,20,10),  (180,60,10)]),
    ("基础设施即代码",           "IaC",              "📋", [(15,15,40),  (50,50,120)]),
    ("故障排查-terway-ip泄漏",   "Incident",         "🚨", [(50,10,10),  (180,20,20)]),
    ("故障排查方法论",           "Debugging",        "🔍", [(40,20,20),  (150,50,50)]),
    ("运维工程师AI工具实践",     "AI × Ops",         "🤖", [(10,40,50),  (20,120,150)]),
    ("零信任网络实践",           "Zero Trust",       "🔒", [(30,30,10),  (100,100,20)]),
]

W, H = 1200, 630

def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

def draw_grid(draw, w, h, color=(255,255,255,15)):
    step = 60
    for x in range(0, w, step):
        draw.line([(x, 0), (x, h)], fill=color, width=1)
    for y in range(0, h, step):
        draw.line([(0, y), (w, y)], fill=color, width=1)

def draw_circles(draw, w, h, base_color):
    # decorative circles in corner
    cx, cy = w - 180, h - 120
    r = 180
    for i in range(4):
        alpha = 30 - i * 7
        c = base_color[:3] + (alpha,)
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=c, width=2)
        r += 60

def make_featured(post_dir, label, icon, colors):
    c1, c2 = colors

    # Gradient background
    img = Image.new("RGB", (W, H), c1)
    draw = ImageDraw.Draw(img, "RGBA")

    # Horizontal gradient
    for y in range(H):
        t = y / H
        for x in range(W):
            tx = x / W
            # diagonal gradient
            blend = (t + tx) / 2
            color = lerp_color(c1, c2, blend)
            img.putpixel((x, y), color)

    draw = ImageDraw.Draw(img, "RGBA")

    # Grid lines
    draw_grid(draw, W, H, (255, 255, 255, 18))

    # Decorative circles
    draw_circles(draw, W, H, c2 + (50,))

    # Left accent bar
    draw.rectangle([(0, 0), (8, H)], fill=(255, 255, 255, 80))

    # Top accent bar
    draw.rectangle([(0, 0), (W, 6)], fill=(255, 255, 255, 60))

    # Bottom accent bar
    draw.rectangle([(0, H - 6), (W, H)], fill=(255, 255, 255, 60))

    # Try to load fonts
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    font_regular_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf",
    ]

    def load_font(paths, size):
        for p in paths:
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except:
                    continue
        return ImageFont.load_default()

    font_large = load_font(font_paths, 90)
    font_medium = load_font(font_paths, 48)
    font_small = load_font(font_regular_paths, 28)

    # Main label text
    text_x, text_y = 80, H // 2 - 60
    # Shadow
    draw.text((text_x + 3, text_y + 3), label, font=font_large, fill=(0, 0, 0, 120))
    # Main text
    draw.text((text_x, text_y), label, font=font_large, fill=(255, 255, 255, 240))

    # Subtitle: socake.github.io
    sub_text = "socake.github.io · DevOps Engineer"
    draw.text((text_x, text_y + 110), sub_text, font=font_small, fill=(255, 255, 255, 160))

    # Tag line bottom left
    tag = "Kubernetes · AWS · GitOps · SRE"
    draw.text((80, H - 70), tag, font=font_small, fill=(255, 255, 255, 120))

    # Save
    out_dir = f"/home/ubuntu/socake-site/content/posts/{post_dir}"
    os.makedirs(out_dir, exist_ok=True)
    out_path = f"{out_dir}/featured.jpg"
    img.save(out_path, "JPEG", quality=92)
    print(f"✓ {out_path}")


if __name__ == "__main__":
    for post_dir, label, icon, colors in POSTS:
        post_path = f"/home/ubuntu/socake-site/content/posts/{post_dir}"
        if os.path.isdir(post_path):
            make_featured(post_dir, label, icon, colors)
        else:
            print(f"⚠ skip (not found): {post_dir}")

    print("\nDone!")
