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
    # New articles - ELK Stack
    ("filebeat-logstash-pipeline", "Filebeat/Logstash","📥", [(20,40,60),  (30,120,180)]),
    ("kibana-visualization-guide", "Kibana",           "📈", [(50,10,50),  (160,40,160)]),
    ("elasticsearch-dsl-query",    "ES DSL",           "🔎", [(10,40,50),  (20,130,160)]),
    ("elk-prometheus-monitoring",  "ELK Monitor",      "🔭", [(40,20,50),  (130,50,160)]),
    # New articles - Prometheus Advanced
    ("prometheus-process-monitoring","Process Exporter","📡",[(20,40,20),  (50,150,50)]),
    ("alertmanager-webhook-api",   "Alert Webhook",    "📮", [(50,20,20),  (180,50,50)]),
    ("prometheus-alert-with-image","Alert Image",      "🖼", [(40,30,10),  (160,100,20)]),
    ("elastic-agent-fleet",        "Elastic Fleet",    "🚢", [(10,30,60),  (20,90,180)]),
    # New articles - Python + Database
    ("python-elasticsearch-client","Python ES",        "🐍", [(10,30,40),  (20,100,140)]),
    ("python-prometheus-monitoring","Python Metrics",  "📊", [(20,40,20),  (60,140,60)]),
    ("celery-async-tasks",         "Celery",           "⚙",  [(50,30,10),  (180,100,20)]),
    ("mongodb-ops-practice",       "MongoDB",          "🍃", [(10,40,20),  (20,140,60)]),
    ("aliyun-sdk-ops",             "Aliyun SDK",       "☁",  [(10,20,50),  (20,60,180)]),
    # New articles - 2026 Trending
    ("ollama-kubernetes-llm",      "Ollama K8s",       "🦙", [(30,10,50),  (100,30,180)]),
    ("mcp-protocol-devops",        "MCP Protocol",     "🔗", [(20,30,50),  (50,100,180)]),
    # New articles - Security + Infra + Career
    ("vault-external-secrets",     "Vault ESO",        "🔐", [(30,20,10),  (120,70,10)]),
    ("trivy-cosign-supply-chain",  "Trivy/Cosign",     "🛡", [(20,40,10),  (60,150,30)]),
    ("victoriametrics-prometheus",  "VictoriaMetrics", "📉", [(10,30,50),  (20,90,170)]),
    ("opentofu-terraform-practice","OpenTofu",         "🏗", [(30,20,40),  (100,60,150)]),
    ("nginx-ops-complete",         "Nginx",            "⚡",  [(10,40,30),  (20,150,80)]),
    ("devops-senior-interview",    "Sr. Interview",    "💼", [(40,10,30),  (150,30,110)]),
    # Wave 2 - Trending
    ("ebpf-observability",         "eBPF",             "🔬", [(10,30,50),  (20,100,180)]),
    ("kubernetes-gateway-api",     "Gateway API",      "🌐", [(20,40,30),  (60,150,90)]),
    ("crossplane-gitops-cloud",    "Crossplane",       "☁",  [(30,20,50),  (100,60,180)]),
    # Wave 3 - K8s Deep Dive
    ("helm-engineering-practice",  "Helm",             "⛵", [(20,30,50),  (40,100,180)]),
    ("istio-service-mesh-practice","Istio",            "🕸",  [(30,10,50),  (110,30,180)]),
    ("kubernetes-rbac-security",   "K8s RBAC",         "🔑", [(40,10,20),  (160,30,80)]),
    ("kubernetes-storage-practice","K8s Storage",      "💾", [(20,30,40),  (50,110,160)]),
    ("slo-sli-error-budget-practice","SLO/Error Budget","📐", [(10,40,30),  (20,160,90)]),
    # Wave 3 - Ops Advanced
    ("chaos-mesh-practice",        "Chaos Mesh",       "🌪",  [(50,10,20),  (200,30,70)]),
    ("opa-kyverno-admission-control","OPA/Kyverno",    "🛂", [(30,20,40),  (110,60,160)]),
    ("go-kubernetes-client-tools", "Go + K8s",         "🐹", [(10,40,30),  (20,160,100)]),
    ("k6-load-testing-practice",   "k6 Load Test",     "📈", [(40,20,10),  (170,70,20)]),
    ("coredns-troubleshooting-guide","CoreDNS",        "🌐", [(20,30,50),  (50,100,200)]),
    ("tcp-network-troubleshooting","TCP/IP",           "🔌", [(10,20,50),  (20,60,200)]),
    # Wave 3 - AI Foundation
    ("llm-landscape-2025",         "LLM 2026",         "🧠", [(20,10,50),  (80,30,200)]),
    ("prompt-engineering-guide",   "Prompt Eng.",      "✍",  [(30,20,50),  (120,60,200)]),
    ("rag-system-design-practice", "RAG",              "🔍", [(10,30,50),  (30,110,190)]),
    ("langchain-practical-guide",  "LangChain",        "⛓",  [(20,30,40),  (60,120,170)]),
    ("claude-api-development-guide","Claude API",      "🤖", [(20,10,40),  (80,30,170)]),
    ("openai-api-engineering",     "OpenAI API",       "⚡",  [(10,20,50),  (30,70,210)]),
    # Wave 3 - AI Tools
    ("cursor-ai-editor-guide",     "Cursor",           "🖱",  [(30,20,50),  (110,60,200)]),
    ("github-copilot-engineering", "Copilot",          "🐙", [(10,20,40),  (30,70,180)]),
    ("claude-code-cli-guide",      "Claude Code",      "💻", [(20,10,50),  (80,30,200)]),
    ("dify-self-hosted-rag-practice","Dify",           "🧩", [(10,40,30),  (30,170,110)]),
    ("fastgpt-knowledge-base-practice","FastGPT",      "⚡",  [(30,10,50),  (120,30,200)]),
    ("ai-agent-design-patterns",   "AI Agent",         "🤖", [(10,30,50),  (30,110,200)]),
    # Wave 3 - AI Frontier
    ("milvus-vector-database-practice","Milvus",       "🗃",  [(20,30,50),  (60,110,200)]),
    ("langfuse-llm-observability", "Langfuse",         "📊", [(30,20,50),  (110,60,200)]),
    ("llm-finetuning-lora-practice","LoRA Fine-tune",  "🎯", [(40,10,30),  (170,30,110)]),
    ("multimodal-llm-vision-practice","Multimodal",    "👁",  [(10,30,50),  (30,110,200)]),
    ("comfyui-stable-diffusion-workflow","ComfyUI/SD", "🎨", [(40,10,40),  (170,30,170)]),
    ("langgraph-workflow-orchestration","LangGraph",   "🔄", [(20,30,50),  (60,110,200)]),
    # Wave 5 - 路线图深度补充文章
    ("linux-system-admin-devops",  "Linux Admin",      "🐧", [(15,20,40),  (40,60,140)]),
    ("kubernetes-resource-management","K8s Resources", "⚖",  [(10,30,50),  (20,90,170)]),
    ("kubernetes-networking-deep-dive","K8s Network",  "🌐", [(20,30,50),  (50,100,180)]),
    ("on-call-engineering-practice","On-Call",         "📟", [(40,20,10),  (160,70,20)]),
    ("sre-incident-management",    "Incident Mgmt",    "🚨", [(50,10,10),  (190,25,25)]),
    ("prometheus-error-budget-alerting","Burn Rate",   "🔥", [(50,20,10),  (200,70,10)]),
    ("embedding-model-selection-guide","Embeddings",   "🧬", [(20,30,50),  (50,110,190)]),
    ("advanced-rag-techniques",    "Advanced RAG",     "🔎", [(10,30,50),  (20,110,180)]),
    ("llm-tool-use-function-calling","Tool Use",       "🔧", [(30,20,50),  (110,60,200)]),
    ("llm-production-serving-vllm","vLLM Serving",     "⚡",  [(30,10,50),  (120,30,200)]),
    ("llm-security-guardrails",    "LLM Security",     "🛡",  [(40,10,20),  (160,30,80)]),
    ("llm-cost-optimization",      "LLM Cost",         "💰", [(20,40,10),  (60,160,30)]),
    ("argocd-advanced-patterns",   "ArgoCD Adv.",      "⚙",  [(20,30,50),  (50,100,190)]),
    ("kubernetes-upgrade-strategy","K8s Upgrade",      "⬆",  [(10,30,50),  (20,90,180)]),
    ("docker-compose-dev-workflow","Compose Dev",      "🐳", [(10,30,60),  (20,90,190)]),
    ("dora-metrics-platform-engineering","DORA",       "📊", [(30,10,40),  (120,30,170)]),
    # Wave 4 - 路线图支撑文章
    ("shell-script-automation",    "Shell Script",     "🖥",  [(20,40,20),  (50,160,50)]),
    ("git-workflow-practice",      "Git Workflow",     "🌿", [(10,40,20),  (20,170,70)]),
    ("kubernetes-beginner-guide",  "K8s 入门",         "☸",  [(10,30,60),  (20,90,220)]),
    ("sre-concepts-and-principles","SRE 理念",         "🔧", [(30,10,40),  (120,30,170)]),
    ("observability-three-pillars","三支柱",           "🔭", [(30,20,50),  (110,60,200)]),
    ("platform-engineering-practice","平台工程",       "🏗", [(20,30,50),  (60,110,200)]),
    ("llm-core-concepts",          "LLM 基础",         "🧠", [(20,10,50),  (80,30,200)]),
    ("python-async-programming",   "Python Async",     "⚡",  [(10,40,20),  (20,170,80)]),
    ("rag-evaluation-ragas",       "RAG 评估",         "📐", [(30,10,50),  (130,30,200)]),
    ("multi-cluster-k8s-management","多集群运维",      "🌐", [(10,30,60),  (20,100,230)]),
    # Wave 6 - 对标顶级博客补充
    ("bpftrace-performance-debug", "bpftrace",         "🔬", [(10,20,50),  (20,60,200)]),
    ("linux-flame-graph-practice", "Flame Graph",      "🔥", [(50,10,10),  (210,50,10)]),
    ("istio-ambient-mesh-practice","Istio Ambient",    "🕸",  [(20,10,50),  (80,30,200)]),
    ("webassembly-cloud-native",   "WebAssembly",      "🦀", [(30,10,50),  (120,30,200)]),
    ("kubernetes-multitenancy-deep-dive","K8s 多租户", "🏢", [(20,30,50),  (50,110,200)]),
    ("kubernetes-operator-development","K8s Operator", "⚙",  [(10,30,50),  (20,100,180)]),
    ("postgresql-ha-patroni",      "PostgreSQL HA",    "🐘", [(10,30,50),  (20,100,180)]),
    ("mysql-ha-mgr-proxysql",      "MySQL HA",         "🐬", [(20,40,10),  (60,170,30)]),
    ("finops-kubernetes-cost-governance","FinOps",     "💰", [(10,40,20),  (20,170,70)]),
    ("headscale-zero-trust-vpn",   "Headscale VPN",    "🔒", [(30,20,40),  (120,60,170)]),
    ("ingress-to-gateway-api-migration","Gateway API", "🌐", [(10,30,50),  (20,100,200)]),
    ("kubernetes-cgroup-v2-migration","cgroup v2",     "⚙",  [(20,30,40),  (70,120,170)]),
    ("grpc-microservices-practice","gRPC",             "⚡",  [(30,10,50),  (130,20,200)]),
    ("argo-workflows-practice",    "Argo Workflows",   "🔄", [(20,30,50),  (60,110,200)]),
    ("service-mesh-comparison",    "Service Mesh",     "🕸",  [(10,20,50),  (30,70,210)]),
    ("use-method-performance-analysis","USE Method",   "📊", [(40,20,10),  (170,70,20)]),
    ("container-image-build-optimization","镜像优化",  "🐳", [(10,30,60),  (20,90,200)]),
    ("linux-kernel-network-tuning","网络调优",         "🌐", [(10,20,50),  (20,60,200)]),
    ("kubernetes-v133-features",   "K8s v1.33",        "☸",  [(10,30,60),  (20,100,230)]),
    ("opencost-kubernetes-cost-visibility","OpenCost", "💸", [(20,40,10),  (70,170,40)]),
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
