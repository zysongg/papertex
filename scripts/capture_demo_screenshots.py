#!/usr/bin/env python3
"""Generate demo screenshots for papertex README using Playwright with mocked APIs."""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

DEMO_DATA = {
    "/api/status": {
        "configured": True, "ssh": True, "ssh_error": "",
        "local_library": "/Users/demo/PaperLibrary", "local_exists": True,
        "remote_library": "/home/researcher/paper-tex", "remote_name": "科研服务器",
        "ssh_alias": "research-server", "ssh_target": "10.0.1.50"
    },
    "/api/settings": {
        "host": "127.0.0.1", "port": 8817,
        "local_library": "/Users/demo/PaperLibrary",
        "review_space": "/Users/demo/ReviewSpace",
        "remote_name": "科研服务器", "connection": "alias",
        "ssh_alias": "research-server", "ssh_host": "", "ssh_user": "", "ssh_port": 22, "ssh_key": "",
        "remote_library": "/home/researcher/paper-tex"
    },
    "/api/pdf-token": {"token": "demo-token"},
    "/api/stars": {"items": ["transformers/2017/attention-is-all-you-need", "diffusion/2020/ddpm"], "count": 2},
    "/api/library?source=local": {
        "source": "local", "count": 6, "items": [
            {"key": "transformers/2017/attention-is-all-you-need", "topic": "transformers", "year": "2017",
             "slug": "attention-is-all-you-need", "has_pdf": True, "pdf": "main.pdf",
             "has_source": False, "has_meta": True, "has_note": True,
             "title": "Attention Is All You Need", "entry": "main.tex", "venue": "NeurIPS",
             "code": "https://github.com/google-research/transformers", "openreview": "",
             "doi": "10.48550/arXiv.1706.03762", "added_at": "2024-03-15T10:00:00Z"},
            {"key": "transformers/2018/bert-pre-training", "topic": "transformers", "year": "2018",
             "slug": "bert-pre-training", "has_pdf": True, "pdf": "main.pdf",
             "has_source": False, "has_meta": True, "has_note": True,
             "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
             "entry": "paper.tex", "venue": "NAACL", "code": "", "openreview": "",
             "doi": "10.18653/v1/N19-1423", "added_at": "2024-03-16T10:00:00Z"},
            {"key": "transformers/2020/gpt-3-language-models", "topic": "transformers", "year": "2020",
             "slug": "gpt-3-language-models", "has_pdf": True, "pdf": "main.pdf",
             "has_source": False, "has_meta": True, "has_note": False,
             "title": "Language Models are Few-Shot Learners", "entry": "paper.tex", "venue": "NeurIPS",
             "code": "", "openreview": "", "doi": "", "added_at": "2024-04-01T10:00:00Z"},
            {"key": "diffusion/2020/ddpm", "topic": "diffusion", "year": "2020",
             "slug": "ddpm", "has_pdf": True, "pdf": "main.pdf",
             "has_source": True, "has_meta": True, "has_note": True,
             "title": "Denoising Diffusion Probabilistic Models", "entry": "main.tex", "venue": "NeurIPS",
             "code": "https://github.com/hojonathanho/diffusion", "openreview": "",
             "doi": "", "added_at": "2024-05-10T10:00:00Z"},
            {"key": "diffusion/2022/stable-diffusion", "topic": "diffusion", "year": "2022",
             "slug": "stable-diffusion", "has_pdf": True, "pdf": "main.pdf",
             "has_source": False, "has_meta": True, "has_note": False,
             "title": "High-Resolution Image Synthesis with Latent Diffusion Models", "entry": "paper.tex",
             "venue": "CVPR", "code": "", "openreview": "", "doi": "", "added_at": "2024-06-20T10:00:00Z"},
            {"key": "gnn/2024/ogb-benchmarks", "topic": "gnn", "year": "2024",
             "slug": "ogb-benchmarks", "has_pdf": True, "pdf": "main.pdf",
             "has_source": False, "has_meta": True, "has_note": False,
             "title": "Open Graph Benchmark: A Platform for ML on Graphs", "entry": "main.tex",
             "venue": "NeurIPS", "code": "", "openreview": "", "doi": "", "added_at": "2024-11-01T10:00:00Z"},
        ]
    },
    "/api/library?source=remote": {
        "source": "remote", "count": 5, "items": [
            {"key": "transformers/2017/attention-is-all-you-need", "topic": "transformers", "year": "2017",
             "slug": "attention-is-all-you-need", "has_pdf": True, "pdf": "main.pdf",
             "has_source": True, "has_meta": True, "has_note": True,
             "title": "Attention Is All You Need", "entry": "main.tex", "venue": "NeurIPS",
             "code": "", "openreview": "", "doi": "", "added_at": "2024-03-15T10:00:00Z"},
            {"key": "transformers/2018/bert-pre-training", "topic": "transformers", "year": "2018",
             "slug": "bert-pre-training", "has_pdf": True, "pdf": "main.pdf",
             "has_source": True, "has_meta": True, "has_note": True,
             "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
             "entry": "paper.tex", "venue": "NAACL", "code": "", "openreview": "", "doi": "",
             "added_at": "2024-03-16T10:00:00Z"},
            {"key": "diffusion/2020/ddpm", "topic": "diffusion", "year": "2020",
             "slug": "ddpm", "has_pdf": True, "pdf": "main.pdf",
             "has_source": True, "has_meta": True, "has_note": True,
             "title": "Denoising Diffusion Probabilistic Models", "entry": "main.tex", "venue": "NeurIPS",
             "code": "", "openreview": "", "doi": "", "added_at": "2024-05-10T10:00:00Z"},
            {"key": "diffusion/2022/stable-diffusion", "topic": "diffusion", "year": "2022",
             "slug": "stable-diffusion", "has_pdf": True, "pdf": "main.pdf",
             "has_source": True, "has_meta": True, "has_note": False,
             "title": "High-Resolution Image Synthesis with Latent Diffusion Models", "entry": "paper.tex",
             "venue": "CVPR", "code": "", "openreview": "", "doi": "", "added_at": "2024-06-20T10:00:00Z"},
            {"key": "gnn/2024/ogb-benchmarks", "topic": "gnn", "year": "2024",
             "slug": "ogb-benchmarks", "has_pdf": True, "pdf": "main.pdf",
             "has_source": False, "has_meta": True, "has_note": False,
             "title": "Open Graph Benchmark: A Platform for ML on Graphs", "entry": "main.tex",
             "venue": "NeurIPS", "code": "", "openreview": "", "doi": "", "added_at": "2024-11-01T10:00:00Z"},
        ]
    },
    "/api/knowledge": {
        "items": [
            {"id": "k1", "key": "_knowledge/k1", "kind": "note", "title": "Diffusion 模型训练技巧笔记",
             "url": "", "source": "remote", "tags": ["training", "tips"], "has_body": True,
             "created_at": "2024-05-12T08:00:00Z", "updated_at": "2024-11-01T09:00:00Z"},
            {"id": "k2", "key": "_knowledge/k2", "kind": "note", "title": "Transformer 架构演进",
             "url": "", "source": "remote", "tags": ["architecture"], "has_body": True,
             "created_at": "2024-03-20T08:00:00Z", "updated_at": "2024-10-15T14:00:00Z"},
            {"id": "k3", "key": "_knowledge/k3", "kind": "link", "title": "PyTorch 分布式训练官方文档",
             "url": "https://pytorch.org/tutorials/intermediate/ddp_tutorial.html",
             "source": "remote", "tags": ["pytorch", "distributed"], "has_body": False,
             "created_at": "2024-06-01T10:00:00Z", "updated_at": "2024-06-01T10:00:00Z"},
            {"id": "k4", "key": "_knowledge/k4", "kind": "link", "title": "LaTeX 符号速查",
             "url": "https://detexify.kirelabs.org/classification.html",
             "source": "remote", "tags": ["latex"], "has_body": False,
             "created_at": "2024-04-10T07:00:00Z", "updated_at": "2024-04-10T07:00:00Z"},
        ],
        "remote_error": ""
    },
    "/api/options": {
        "topics": [
            {"name": "transformers", "count": 3, "years": ["2017", "2018", "2020"]},
            {"name": "diffusion", "count": 2, "years": ["2020", "2022"]},
            {"name": "gnn", "count": 1, "years": ["2024"]},
        ],
        "years": ["2017", "2018", "2020", "2022", "2024"],
        "venues": ["NeurIPS", "NAACL", "CVPR", "ICLR", "ICML"]
    },
    "/api/research": {"items": {}, "revision": "demo-revision"},
    "/api/projects?source=remote": {
        "source": "remote", "items": [
            {"project": "icassp25-ssl", "source": "remote", "exists": True, "has_refs": True, "count": 4},
            {"project": "cvpr25-segmentation", "source": "remote", "exists": True, "has_refs": True, "count": 6},
            {"project": "thesis-draft", "source": "remote", "exists": True, "has_refs": True, "count": 12},
        ]
    },
    "/api/projects?source=local": {
        "source": "local", "items": [
            {"project": "icassp25-ssl", "source": "local", "exists": True, "has_refs": True, "count": 4},
        ]
    },
    "/api/refs?project=icassp25-ssl&source=remote": {
        "project": "icassp25-ssl", "source": "remote",
        "file": "reference-tex.json", "exists": True,
        "library": "/home/researcher/paper-tex",
        "read_order": ["entry", "pdf"],
        "references": [
            {"path": "transformers/2017/attention-is-all-you-need",
             "entry": "source/main.tex", "pdf": "2017-NeurIPS-Vaswani-Attention.pdf",
             "title": "Attention Is All You Need",
             "current": {"entry": "source/main.tex", "pdf": "2017-NeurIPS-Vaswani-Attention.pdf",
                         "title": "Attention Is All You Need",
                         "code": "https://github.com/google-research/transformers"},
             "missing": False, "stale": False, "note": "核心方法参考"},
            {"path": "transformers/2018/bert-pre-training",
             "entry": "source/paper.tex", "pdf": "2018-NAACL-Devlin-BERT.pdf",
             "title": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
             "current": {"entry": "source/paper.tex", "pdf": "2018-NAACL-Devlin-BERT.pdf"},
             "missing": False, "stale": False, "note": ""},
            {"path": "_knowledge/k1", "entry": "note.md",
             "title": "Diffusion 模型训练技巧笔记",
             "current": {"kind": "note", "title": "Diffusion 模型训练技巧笔记"},
             "missing": False, "stale": False, "note": "实验参数参考"},
        ],
        "revision": "demo", "remote_error": ""
    },
    "/api/project/files?project=icassp25-ssl&source=remote&path=&types=any": {
        "files": [
            {"name": "main.tex", "kind": "file", "entry": True, "bytes": 24580, "mtime": "2024-11-02T18:30:00Z"},
            {"name": "related.tex", "kind": "file", "entry": False, "bytes": 8900, "mtime": "2024-11-01T09:00:00Z"},
            {"name": "figures/", "kind": "dir", "entry": False, "bytes": 0, "mtime": "2024-10-30T14:00:00Z"},
            {"name": "experiments.tex", "kind": "file", "entry": False, "bytes": 12300, "mtime": "2024-10-28T16:00:00Z"},
            {"name": "notes.md", "kind": "file", "entry": False, "bytes": 3400, "mtime": "2024-10-25T11:00:00Z"},
        ],
        "path": "", "root": "/home/researcher/paper-tex/projects/icassp25-ssl"
    },
    "/api/project/recent?project=icassp25-ssl&source=remote&types=any": {
        "files": [
            {"name": "main.tex", "kind": "file", "entry": True, "bytes": 24580, "mtime": "2024-11-02T18:30:00Z"},
            {"name": "related.tex", "kind": "file", "entry": False, "bytes": 8900, "mtime": "2024-11-01T09:00:00Z"},
            {"name": "experiments.tex", "kind": "file", "entry": False, "bytes": 12300, "mtime": "2024-10-28T16:00:00Z"},
        ],
        "path": "", "root": "/home/researcher/paper-tex/projects/icassp25-ssl"
    },
    "/api/review": {
        "count": 3,
        "items": [
            {"venue": "ICASSP 2025", "submission": "Self-Supervised Pre-training for Audio",
             "key": "r1", "title": "SSP-SSL: Masked Spectrogram Modeling", "venue_label": "ICASSP 2025",
             "round": "R1", "due": "2024-11-20", "status": "drafting", "rating": "5",
             "confidence": "4", "note": "Strengths: novel masking strategy...", "purged_at": "",
             "status_label": "撰写中", "days_left": 13, "overdue": False, "has_manuscript": True},
            {"venue": "CVPR 2025", "submission": "Real-Time Semantic Segmentation",
             "key": "r2", "title": "FastSeg: Light-Weight Segmentation for Edge Devices",
             "venue_label": "CVPR 2025", "round": "R1", "due": "2024-12-05", "status": "reviewing",
             "rating": "", "confidence": "", "note": "", "purged_at": "",
             "status_label": "审稿中", "days_left": 28, "overdue": False, "has_manuscript": True},
            {"venue": "NeurIPS 2024", "submission": "Graph Neural Networks Benchmark",
             "key": "r3", "title": "OGB-Large: Scalable GNN Training",
             "venue_label": "NeurIPS 2024", "round": "Rebuttal", "due": "2024-10-10", "status": "done",
             "rating": "7", "confidence": "4", "note": "", "purged_at": "",
             "status_label": "已完成", "days_left": -31, "overdue": True, "has_manuscript": True},
        ],
        "root": "/Users/demo/PaperLibrary/reviews",
        "trash_count": 2,
        "templates": [{"name": "default", "fields": [], "sections": []}],
        "statuses": [
            {"value": "drafting", "label": "撰写中"},
            {"value": "reviewing", "label": "审稿中"},
            {"value": "rebuttal", "label": "Rebuttal"},
            {"value": "done", "label": "已完成"},
        ]
    },
    "/api/review/task?key=r1": {
        "task": {
            "venue": "ICASSP 2025", "submission": "Self-Supervised Pre-training for Audio",
            "key": "r1", "title": "SSP-SSL: Masked Spectrogram Modeling",
            "venue_label": "ICASSP 2025", "round": "R1", "due": "2024-11-20",
            "status": "drafting", "rating": "5", "confidence": "4",
            "note": "Strengths: novel masking strategy, clear ablation study.",
            "purged_at": "", "status_label": "撰写中", "days_left": 13, "overdue": False,
        },
        "review": "# 评审意见\n\n## 总结\n\n本文提出一种基于掩码频谱图建模的自监督音频预训练方法,在多个下游任务上取得稳定提升。\n\n## 优点\n\n- 掩码策略针对音频时频结构设计,动机充分\n- 消融实验覆盖掩码比例、编码器深度与预训练数据规模\n- 写作清晰,实验设置可复现\n\n## 不足\n\n- 与同期 SSL 方法的计算开销对比不足\n- 下游微调的超参数敏感性未充分讨论\n\n## 建议\n\nWeak Accept (5). 建议补充推理成本与低资源场景实验。\n",
        "revision": "demo-review",
        "files": [
            {"name": "manuscript.pdf", "size": 8421376, "mtime": "2024-11-01T09:00:00Z"},
        ],
    },
}

DETAIL_RESPONSES = {
    "/api/knowledge/k1": {
        "source": "remote",
        "item": {
            "id": "k1", "key": "_knowledge/k1", "kind": "note", "title": "Diffusion 模型训练技巧笔记",
            "url": "", "source": "remote", "tags": ["training", "tips"], "has_body": True,
            "created_at": "2024-05-12T08:00:00Z", "updated_at": "2024-11-01T09:00:00Z",
            "body": "# Diffusion 模型训练技巧\n\n## 1. 学习率调度\n\nDDPM 原文使用 **cosine** 调度,相比 linear 调度效果更好：\n\n$$\\eta_t = \\eta_{\\min} + \\frac{1}{2}(\\eta_{\\max} - \\eta_{\\min})(1 + \\cos(\\pi t / T))$$\n\n## 2. 噪声调度\n\n| 调度方式 | 效果 | 适用场景 |\n| --- | --- | --- |\n| Linear | 早期版本,简单 | 图像生成 |\n| Cosine | Improved DDPM 推荐 | 通用 |\n| Sigmoid | 后期改进 | 高质量生成 |\n\n## 3. 训练注意事项\n\n- **EMA** 对推理质量提升显著,推荐 `decay=0.9999`\n- 小批量时 `GroupNorm` 比 `BatchNorm` 更稳定\n- 梯度裁剪 `max_norm=1.0` 防止训练发散",
        }
    },
}


async def capture_screenshots(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    app_root = Path(__file__).resolve().parent.parent.parent
    static_dir = app_root / "app" / "static"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
            locale="zh-CN",
        )

        async def mock_routes(page):
            async def handler(route, request):
                from urllib.parse import urlparse
                parsed = urlparse(request.url)
                path = parsed.path
                key = path + (("?" + parsed.query) if parsed.query else "")
                if key in DEMO_DATA:
                    await route.fulfill(json=DEMO_DATA[key])
                    return
                if path in DEMO_DATA:
                    await route.fulfill(json=DEMO_DATA[path])
                    return
                if path in DETAIL_RESPONSES:
                    await route.fulfill(json=DETAIL_RESPONSES[path])
                    return
                if path in ("/api/project/files", "/api/project/recent"):
                    demo_key = next((k for k in DEMO_DATA if k.startswith(path + "?")), "")
                    await route.fulfill(json=DEMO_DATA[demo_key] if demo_key else {"files": [], "path": "", "root": ""})
                    return
                if path == "/api/refs":
                    await route.fulfill(json=DEMO_DATA["/api/refs?project=icassp25-ssl&source=remote"])
                    return
                if path == "/api/paper":
                    await route.fulfill(json={
                        "local": None, "remote": {
                            "key": "transformers/2017/attention-is-all-you-need", "topic": "transformers",
                            "year": "2017", "slug": "attention-is-all-you-need",
                            "has_pdf": True, "pdf": "main.pdf", "has_source": True,
                            "has_meta": True, "has_note": True, "entry": "main.tex",
                            "title": "Attention Is All You Need", "venue": "NeurIPS",
                            "code": "https://github.com/google-research/transformers",
                            "openreview": "", "doi": "10.48550/arXiv.1706.03762",
                            "tags": ["attention", "nlp"],
                        }, "status": "remote-only"
                    })
                    return
                if path == "/api/paper/note":
                    await route.fulfill(json={
                        "text": "# Attention Is All You Need\n\n## 核心贡献\n\n提出了 **Transformer** 架构,完全基于注意力机制,摒弃了循环与卷积。\n\n## 关键要点\n\n- **Self-Attention** 取代了 RNN 的时序建模\n- Multi-Head Attention 并行捕获多子空间关系\n- 位置编码(Pe)提供序列顺序信息\n\n## 训练细节\n\n- Adam 优化器,$\\beta_1=0.9,\\beta_2=0.98$\n- Warmup 4000 步 + 衰减\n- Dropout $0.1$,Label Smoothing $0.1$",
                        "revision": "demo-revision", "source": "remote",
                        "has_note": True, "byte_size": 420, "recovery_text": ""
                    })
                    return
                await route.fulfill(status=404, json={"detail": "not mocked"})

            await page.route("**/api/**", handler)

        page = await context.new_page()
        page.on("pageerror", lambda e: print("PAGEERROR", e))
        page.on("console", lambda m: print("CONSOLE", m.type, m.text) if m.type == "error" else None)
        await mock_routes(page)

        # Screenshot 1: Library view
        await page.goto("http://127.0.0.1:8817/#library", wait_until="domcontentloaded")
        await page.wait_for_timeout(600)
        await page.screenshot(path=str(out_dir / "library.png"))
        print("captured library.png")
        print("LIB TEXT:", (await page.locator("body").inner_text())[:900].replace("\n", " | "))

        # Screenshot 2: Paper detail with note
        # Click first paper row
        try:
            await page.wait_for_selector("#list .item[data-key]", timeout=5000)
            paper = page.locator('#list .item[data-key="transformers/2017/attention-is-all-you-need"]')
            if not await paper.count():
                paper = page.locator("#list .item[data-key]").first
            print("paper items:", await page.locator("#list .item[data-key]").count())
            await paper.click()
            await page.wait_for_timeout(900)
            await page.screenshot(path=str(out_dir / "paper-detail.png"))
            print("captured paper-detail.png")
            print("DETAIL TEXT:", (await page.locator("#detail").inner_text())[:900].replace("\n", " | "))
        except Exception as exc:
            print("paper-detail failed:", exc)

        # Screenshot 3: Knowledge notes
        note_tab = page.locator("button[data-tab='library'][data-mode='note']").first
        await note_tab.click()
        await page.wait_for_timeout(600)
        note_row = page.locator("#list .item[data-key]").first
        print("note items:", await page.locator("#list .item[data-key]").count())
        if await note_row.count():
            await note_row.click()
            await page.wait_for_timeout(900)
        await page.screenshot(path=str(out_dir / "knowledge.png"))
        print("captured knowledge.png")
        print("KNOW TEXT:", (await page.locator("body").inner_text())[:900].replace("\n", " | "))

        # Screenshot 4: Review workspace
        await page.locator("button[data-tab='review']").first.click()
        await page.wait_for_timeout(600)
        review_row = page.locator('#review-list [data-review-key="r1"]')
        if await review_row.count():
            await review_row.click()
            await page.wait_for_timeout(700)
        await page.screenshot(path=str(out_dir / "review.png"))
        print("captured review.png")
        print("REVIEW TEXT:", (await page.locator("body").inner_text())[:900].replace("\n", " | "))

        # Screenshot 5: Projects/refs
        await page.locator("button[data-tab='refs']").first.click()
        await page.wait_for_timeout(800)
        proj_btn = page.locator("#project-list .item").first
        if await proj_btn.count():
            await proj_btn.click()
            await page.wait_for_timeout(600)
        await page.screenshot(path=str(out_dir / "projects.png"))
        print("captured projects.png")
        print("PROJ TEXT:", (await page.locator("body").inner_text())[:900].replace("\n", " | "))

        await browser.close()


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "docs" / "screenshots"
    asyncio.run(capture_screenshots(out))
    print("done ->", out)
