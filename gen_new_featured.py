#!/usr/bin/env python3
"""Generate featured images for all posts missing featured.jpg (fast, numpy)."""

import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
POSTS_DIR = "/home/ubuntu/socake-site/content/posts"

# (slug, label, color_scheme)
POSTS = [
    # Agent1 - Database / Storage
    ("tidb-production-practice",          "TiDB",             [(10,30,50),  (20,90,170)]),
    ("mysql-performance-tuning-deep-dive","MySQL Tuning",     [(20,30,60),  (30,90,180)]),
    ("postgresql-vacuum-bloat-tuning",    "PG VACUUM",        [(10,30,40),  (20,110,150)]),
    ("redis-cluster-migration",           "Redis Cluster",    [(50,10,10),  (180,30,30)]),
    ("mongodb-sharding-practice",         "MongoDB Shard",    [(10,40,20),  (20,140,60)]),
    ("minio-distributed-storage",         "MinIO",            [(30,20,10),  (180,80,20)]),
    ("ceph-rook-kubernetes",              "Rook Ceph",        [(20,30,50),  (60,90,180)]),
    ("vitess-mysql-sharding",             "Vitess",           [(10,30,60),  (30,110,200)]),
    ("database-change-management",        "DB Migration",     [(30,20,40),  (110,60,160)]),
    ("columnar-warehouse-doris-starrocks","Doris/StarRocks",  [(40,20,10),  (170,80,20)]),
    # Agent2 - K8s advanced
    ("keda-event-driven-autoscaling",     "KEDA",             [(10,30,50),  (30,120,200)]),
    ("cert-manager-production",           "cert-manager",     [(10,40,30),  (20,150,90)]),
    ("external-dns-multi-provider",       "ExternalDNS",      [(20,30,50),  (60,100,180)]),
    ("karmada-multi-cluster",             "Karmada",          [(30,10,50),  (110,30,180)]),
    ("vcluster-virtual-cluster",          "vcluster",         [(20,30,50),  (50,110,190)]),
    ("kueue-batch-workload",              "Kueue",            [(10,40,50),  (20,130,170)]),
    ("descheduler-workload-rebalance",    "Descheduler",      [(40,30,10),  (170,110,20)]),
    ("kubevirt-vm-on-kubernetes",         "KubeVirt",         [(20,20,50),  (60,60,180)]),
    ("cluster-api-infrastructure",        "Cluster API",      [(10,30,50),  (20,100,180)]),
    ("kubernetes-admission-webhook-dev",  "Admission",        [(40,10,40),  (160,40,160)]),
    # Agent3 - Observability
    ("loki-architecture-deep-dive",       "Loki",             [(40,20,10),  (170,80,20)]),
    ("grafana-mimir-long-term-metrics",   "Mimir",            [(40,20,30),  (160,70,110)]),
    ("pyroscope-continuous-profiling",    "Pyroscope",        [(50,10,30),  (200,40,100)]),
    ("grafana-tempo-distributed-tracing", "Tempo",            [(30,20,50),  (120,60,180)]),
    ("ebpf-network-observability-cilium-hubble","Hubble eBPF",[(10,30,50),  (20,100,180)]),
    ("kiali-service-mesh-observability",  "Kiali",            [(20,30,40),  (60,110,160)]),
    ("chaos-engineering-gameday",         "Chaos",            [(50,10,20),  (200,30,70)]),
    ("incident-response-postmortem",      "Postmortem",       [(40,20,20),  (160,60,60)]),
    ("oncall-rotation-management",        "On-Call",          [(30,20,10),  (140,80,20)]),
    ("metric-cardinality-governance",     "Cardinality",      [(40,20,40),  (150,60,150)]),
    # Agent4 - Security
    ("falco-runtime-security-deep",       "Falco",            [(40,20,10),  (170,80,20)]),
    ("spiffe-spire-workload-identity",    "SPIFFE",           [(20,30,50),  (60,100,180)]),
    ("sigstore-cosign-signing-workflow",  "Sigstore",         [(30,20,40),  (120,70,160)]),
    ("sbom-dependency-track",             "SBOM",             [(10,40,30),  (20,140,80)]),
    ("cilium-network-policy-production",  "Cilium NP",        [(30,20,10),  (180,100,20)]),
    ("wireguard-mesh-vpn",                "WireGuard",        [(40,10,30),  (170,30,110)]),
    ("secret-rotation-automation",        "Secret Rotate",    [(30,20,10),  (150,80,20)]),
    ("kubernetes-pod-security-standards", "Pod Security",     [(40,10,20),  (160,40,80)]),
    ("kyverno-policy-as-code",            "Kyverno",          [(20,30,50),  (60,110,180)]),
    ("supply-chain-slsa-framework",       "SLSA",             [(10,40,20),  (20,150,60)]),
    # Agent5 - DevOps tooling
    ("buildkit-cache-production",         "BuildKit",         [(30,20,10),  (160,90,20)]),
    ("ko-go-image-build",                 "ko Go",            [(10,40,30),  (20,160,100)]),
    ("tekton-pipelines-production",       "Tekton",           [(20,30,50),  (60,110,190)]),
    ("dagger-programmable-cicd",          "Dagger",           [(30,20,40),  (120,70,170)]),
    ("nix-devcontainer-reproducible-env", "Nix Dev",          [(10,30,50),  (30,110,190)]),
    ("earthly-buildfile-monorepo",        "Earthly",          [(30,20,30),  (140,80,100)]),
    ("pulumi-vs-terraform",               "Pulumi/TF",        [(30,20,50),  (120,60,180)]),
    ("terragrunt-terraform-at-scale",     "Terragrunt",       [(30,10,40),  (140,40,170)]),
    ("renovate-bot-dependency-upgrade",   "Renovate",         [(20,40,20),  (60,150,70)]),
    ("release-automation-changelog",      "Release Auto",     [(40,20,40),  (160,80,150)]),
    # Agent6 - LLM / AI
    ("vllm-multi-node-distributed",       "vLLM",             [(20,10,50),  (80,30,200)]),
    ("tensorrt-llm-inference",            "TensorRT-LLM",     [(20,30,10),  (60,150,30)]),
    ("triton-inference-server-production","Triton",           [(30,20,40),  (120,70,160)]),
    ("sglang-structured-generation",      "SGLang",           [(40,10,30),  (160,40,110)]),
    ("llamafactory-finetuning",           "LLaMA Factory",    [(30,20,10),  (150,90,20)]),
    ("unsloth-efficient-finetuning",      "Unsloth",          [(40,20,10),  (170,80,20)]),
    ("volcano-gpu-batch-scheduling",      "Volcano",          [(40,10,20),  (160,40,80)]),
    ("ray-serve-model-deployment",        "Ray Serve",        [(20,30,50),  (60,110,190)]),
    ("litellm-gateway-proxy",             "LiteLLM",          [(30,20,50),  (110,70,180)]),
    ("autogen-multi-agent-practice",      "AutoGen",          [(10,30,50),  (30,110,190)]),
    # Leftover pre-existing posts also missing images
    ("backstage-developer-portal",        "Backstage",        [(20,30,50),  (60,110,180)]),
    ("devsecops-practice",                "DevSecOps",        [(30,20,40),  (130,70,160)]),
    ("distributed-tracing-jaeger-tempo",  "Jaeger/Tempo",     [(30,20,50),  (120,60,180)]),
    ("harbor-registry-ops",               "Harbor",           [(10,30,50),  (20,100,180)]),
    ("kubernetes-gpu-scheduling",         "K8s GPU",          [(40,10,20),  (160,40,80)]),
    ("kubernetes-network-policy",         "K8s NetPol",       [(20,30,50),  (60,100,180)]),
    ("postgresql-ops-practice",           "PostgreSQL",       [(10,30,50),  (20,100,170)]),
    ("rabbitmq-ops-practice",             "RabbitMQ",         [(40,20,10),  (180,90,20)]),
    ("zookeeper-ops-practice",            "Zookeeper",        [(30,20,40),  (120,70,160)]),
]


def lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def make_gradient(c1, c2):
    c1 = np.array(c1, dtype=np.float32)
    c2 = np.array(c2, dtype=np.float32)
    ys = np.linspace(0, 1, H).reshape(H, 1)
    xs = np.linspace(0, 1, W).reshape(1, W)
    blend = (ys + xs) / 2
    blend = blend[:, :, None]
    img = c1 * (1 - blend) + c2 * blend
    return Image.fromarray(img.astype(np.uint8), "RGB")


def draw_grid(draw, color=(255, 255, 255, 18)):
    for x in range(0, W, 60):
        draw.line([(x, 0), (x, H)], fill=color, width=1)
    for y in range(0, H, 60):
        draw.line([(0, y), (W, y)], fill=color, width=1)


def draw_circles(draw, base_color):
    cx, cy = W - 180, H - 120
    r = 180
    for i in range(4):
        alpha = 30 - i * 7
        c = base_color[:3] + (alpha,)
        draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=c, width=2)
        r += 60


def load_font(paths, size):
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf",
]
FONT_REG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def make_featured(slug, label, colors):
    c1, c2 = colors
    img = make_gradient(c1, c2)
    draw = ImageDraw.Draw(img, "RGBA")
    draw_grid(draw)
    draw_circles(draw, c2 + (50,))
    draw.rectangle([(0, 0), (8, H)], fill=(255, 255, 255, 80))
    draw.rectangle([(0, 0), (W, 6)], fill=(255, 255, 255, 60))
    draw.rectangle([(0, H - 6), (W, H)], fill=(255, 255, 255, 60))

    font_large = load_font(FONT_BOLD, 90)
    font_small = load_font(FONT_REG, 28)

    tx, ty = 80, H // 2 - 60
    draw.text((tx + 3, ty + 3), label, font=font_large, fill=(0, 0, 0, 120))
    draw.text((tx, ty), label, font=font_large, fill=(255, 255, 255, 240))
    draw.text((tx, ty + 110), "socake.github.io · DevOps Engineer",
              font=font_small, fill=(255, 255, 255, 160))
    draw.text((80, H - 70), "Kubernetes · AWS · GitOps · SRE",
              font=font_small, fill=(255, 255, 255, 120))

    out_dir = os.path.join(POSTS_DIR, slug)
    if not os.path.isdir(out_dir):
        print(f"⚠ skip (not found): {slug}")
        return
    out_path = os.path.join(out_dir, "featured.jpg")
    img.save(out_path, "JPEG", quality=92)
    print(f"✓ {slug}")


if __name__ == "__main__":
    for slug, label, colors in POSTS:
        make_featured(slug, label, colors)
    print(f"\nDone: {len(POSTS)} images.")
