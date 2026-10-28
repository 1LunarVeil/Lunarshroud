"""Measure retrieval quality on the real 377-chunk legal index.

Runs three configurations over one labelled question set so the numbers are
comparable: dense-only, hybrid (RRF of dense + FTS5 trigram, which is what
ships), and hybrid + cross-encoder rerank.

Ground truth is a (document, regex) pair per question. Every gold is validated
against the indexed chunk text before measuring, so a question whose answer is
not actually in the index aborts the run instead of silently counting as a miss.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLAMA = os.environ.get("LUNARSHROUD_LLAMA_SERVER", "")
EMBED = os.environ.get("LUNARSHROUD_TEST_EMBEDDING", "")
RERANK = os.environ.get("LUNARSHROUD_TEST_RERANK", "")
KB = os.environ.get("LUNARSHROUD_TEST_KB", "")
EMBED_PORT = 18099
RERANK_PORT = 18100

QUESTIONS = [
    {
        "id": "principles",
        "kind": "definition",
        "q": "处理个人信息应当遵循哪些基本原则？",
        "doc": "中华人民共和国个人信息保护法.pdf",
        "gold": r"第五条\s*处理个人信息应当遵循合法、正当、必要和诚信原则",
    },
    {
        "id": "refuse_service",
        "kind": "article",
        "q": "个人不同意处理其个人信息，处理者能否据此拒绝提供产品或者服务？",
        "doc": "中华人民共和国个人信息保护法.pdf",
        "gold": r"第十六条\s*个人信息处理者不得以个人不同意处理其个人信息",
    },
    {
        "id": "notify_items",
        "kind": "article",
        "q": "处理个人信息前应当以显著方式告知哪些事项？",
        "doc": "中华人民共和国个人信息保护法.pdf",
        "gold": r"第十七条",
    },
    {
        "id": "espionage Acts",
        "kind": "definition",
        "q": "哪些行为属于间谍行为？",
        "doc": "中华人民共和国反间谍法.pdf",
        "gold": r"参加间谍组织或者接受间谍组织及其代理人的任务",
    },
    {
        "id": "national_security_def",
        "kind": "definition",
        "q": "国家安全是指什么？",
        "doc": "中华人民共和国国家安全法.pdf",
        "gold": r"国家安全是指国家政权、主权、统一和领土完整",
    },
    {
        "id": "ai_training",
        "kind": "topic",
        "q": "国家支持人工智能研发时对训练数据和算力有什么规定？",
        "doc": "中华人民共和国国家安全法（2025修订版）.pdf",
        "gold": r"国家支持人工智能基础理论研究和算法等关键技术研发",
    },
    {
        "id": "cipher_effective",
        "kind": "date",
        "q": "中华人民共和国密码法自何时起施行？",
        "doc": "中华人民共和国密码法.pdf",
        "gold": r"自2020年1月1日起施行",
    },
    {
        "id": "cipher_forced",
        "kind": "topic",
        "q": "行政机关能否利用行政手段强制转让商用密码技术？",
        "doc": "中华人民共和国密码法.pdf",
        "gold": r"不得利用行政手段强制转让商用密码技术",
    },
    {
        "id": "data_def",
        "kind": "definition",
        "q": "数据安全法所称数据是指什么？",
        "doc": "中华人民共和国数据安全法.pdf",
        "gold": r"本法所称数据，是指任何以电子或者其他方式对信息的记录",
    },
    {
        "id": "data_coordination",
        "kind": "article",
        "q": "国家数据安全工作由哪个机构负责决策和协调？",
        "doc": "中华人民共和国数据安全法.pdf",
        "gold": r"中央国家安全领导机构负责国家数据安全工作",
    },
    {
        "id": "regulation_effective",
        "kind": "date",
        "q": "网络数据安全管理条例自何时起施行？",
        "doc": "网络数据安全管理条例.docx",
        "gold": r"自2025年1月1日起施行",
    },
    {
        "id": "secret_revision",
        "kind": "date",
        "q": "保守国家秘密法最近一次修订是什么时候？",
        "doc": "中华人民共和国保守国家秘密法_20240227.docx",
        "gold": r"2024年2月27日.*第二次修订",
    },
    {
        "id": "operator_duty",
        "kind": "topic",
        "q": "网络运营者开展经营和服务活动必须履行哪些义务？",
        "doc": "中华人民共和国国家安全法（2025修订版）.pdf",
        "gold": r"履行网络安全保护义务",
    },
    {
        "id": "cross_border",
        "kind": "topic",
        "q": "个人信息跨境提供的规则规定在哪一章？",
        "doc": "中华人民共和国个人信息保护法.pdf",
        "gold": r"第三章\s*个人信息跨境提供的规则",
    },
    {
        "id": "elderly",
        "kind": "topic",
        "q": "数据开发利用要兼顾老年人、残疾人的什么需求？",
        "doc": "中华人民共和国数据安全法.pdf",
        "gold": r"避免对老年人、残疾人的障碍",
    },
    {
        "id": "article17_exact",
        "kind": "lexical",
        "q": "个人信息保护法 第十七条",
        "doc": "中华人民共和国个人信息保护法.pdf",
        "gold": r"第十七条",
    },
    {
        "id": "spy_info_duty",
        "kind": "topic",
        "q": "国家安全机关在反间谍工作中获取的个人和组织信息能用于其他用途吗？",
        "doc": "中华人民共和国反间谍法.pdf",
        "gold": r"国家安全机关及其工作人员依法履行反间谍工作职责获取的个人和组织的信息",
    },
    {
        "id": "cipher_core",
        "kind": "topic",
        "q": "国家对核心密码和普通密码实行什么管理？",
        "doc": "中华人民共和国密码法.pdf",
        "gold": r"国家加强核心密码",
    },
    {
        "id": "ai_ethics",
        "kind": "topic",
        "q": "网络安全法对人工智能伦理规范有什么要求？",
        "doc": "中华人民共和国国家安全法（2025修订版）.pdf",
        "gold": r"完善人工智能伦理规范",
    },
    {
        "id": "data_classified",
        "kind": "topic",
        "q": "国家是否建立数据分类分级保护制度？",
        "doc": "中华人民共和国数据安全法.pdf",
        "gold": r"国家建立数据分类分级保护制度",
    },
    {
        "id": "english_query",
        "kind": "cross-lingual",
        "q": "what principles must a personal information handler follow",
        "doc": "中华人民共和国个人信息保护法.pdf",
        "gold": r"合法、正当、必要和诚信原则",
    },
]

# No answer in this corpus; used to measure how often retrieval confidently
# returns something anyway.
NEGATIVES = [
    {"id": "neg_copyright", "q": "著作权的保护期是多少年？", "gold": None},
    {"id": "neg_traffic", "q": "醉酒驾车会受到什么处罚？", "gold": None},
    {"id": "neg_leasing", "q": "房屋租赁合同纠纷如何解除？", "gold": None},
    {"id": "neg_labor", "q": "劳动合同解除的经济补偿如何计算？", "gold": None},
    {"id": "neg_tax", "q": "个人所得税的起征点是多少元？", "gold": None},
    {"id": "neg_company", "q": "公司股东滥用法人独立地位应承担什么责任？", "gold": None},
    {"id": "neg_admin", "q": "申请行政复议的期限是多久？", "gold": None},
    {"id": "neg_marriage", "q": "夫妻共同财产如何认定和分割？", "gold": None},
    {"id": "neg_consumer", "q": "七天无理由退货有哪些例外情形？", "gold": None},
    {"id": "neg_patent", "q": "发明专利的保护期限是二十年吗？", "gold": None},
]


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 180.0) -> None:
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server on {port} exited with {proc.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"server on {port} did not become ready")


def start(model: str, port: int, extra: list[str], ctx: int) -> subprocess.Popen:
    cmd = [
        LLAMA, "-m", model, "--jinja", "-c", str(ctx), "-ngl", "99",
        "-b", "2048", "-ub", "2048", "-np", "1", "-fa", "on", "--kv-unified",
        "--host", "127.0.0.1", "--port", str(port), *extra,
    ]
    log = open(os.path.join(REPO, "tmp", f"eval-server-{port}.log"), "w", encoding="utf-8")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=REPO)


def main() -> int:
    missing = [name for name, value in (
        ("LUNARSHROUD_LLAMA_SERVER", LLAMA),
        ("LUNARSHROUD_TEST_EMBEDDING", EMBED),
        ("LUNARSHROUD_TEST_KB", KB),
    ) if not value]
    if missing:
        print("set these environment variables first:", ", ".join(missing))
        return 2
    from core import knowledge_retrieval, knowledge_store, llama_services

    names = {d["id"]: d["display_name"] for d in knowledge_store.list_documents(KB)}
    corpus: dict[str, dict[int, str]] = {}
    for document in knowledge_store.list_documents(KB):
        for chunk in knowledge_store.get_document_chunks(KB, document["id"], limit=2000):
            corpus.setdefault(names[document["id"]], {})[int(chunk["ordinal"])] = chunk["text"]

    # --- validate ground truth exists in the index -----------------------
    ungrounded = []
    for item in QUESTIONS:
        texts = corpus.get(item["doc"], {})
        hits = [o for o, t in texts.items() if re.search(item["gold"], t, re.S)]
        if not hits:
            ungrounded.append(f"{item['id']} -> {item['doc']} /{item['gold'][:40]}/")
        else:
            item["gold_ordinals"] = set(hits)
    if ungrounded:
        print("GROUNDING FAILURES (question is not answerable from the index):")
        for line in ungrounded:
            print("  ", line)
        return 2
    print(f"all {len(QUESTIONS)} gold labels are present in the index\n")

    base = knowledge_store.get_base(KB)
    embed_proc = start(EMBED, EMBED_PORT, ["--embeddings"], 2048)
    rerank_proc = None
    try:
        wait_ready(EMBED_PORT, embed_proc)
        embed_url = f"http://127.0.0.1:{EMBED_PORT}"

        def dense(query: str):
            vector = knowledge_retrieval._embedding_query(base, query, embed_url)
            return knowledge_store.search_vectors(KB, vector, limit=30)

        def hybrid(query: str):
            return knowledge_retrieval._fuse(
                dense(query), knowledge_store.search_fts(KB, query, limit=30), min_score=0.0
            )

        rerank_proc = start(RERANK, RERANK_PORT, ["--rerank"], 4096)
        wait_ready(RERANK_PORT, rerank_proc)
        rerank_url = f"http://127.0.0.1:{RERANK_PORT}"

        diversify = knowledge_retrieval._diversify

        modes = {
            "dense": lambda q: dense(q),
            "hybrid": lambda q: hybrid(q),
            "hybrid+div": lambda q: diversify(hybrid(q), limit=20),
            "hybrid+rerank": lambda q: rerank_of(hybrid(q), q),
            "hybrid+div+rerank": lambda q: rerank_of(diversify(hybrid(q), limit=20), q),
        }

        def rerank_of(cands, query):
            if not cands:
                return cands
            scored = llama_services.rerank(rerank_url, query, [c["text"] for c in cands[:20]])
            order = sorted(range(len(scored)), key=lambda i: -scored[i])
            return [cands[i] for i in order]

        documents = {d["id"]: d["display_name"] for d in knowledge_store.list_documents(KB)}

        results = {}
        for name, fn in modes.items():
            stats = {"top1": 0, "top3": 0, "top5": 0, "rr": 0.0, "misses": []}
            for item in QUESTIONS:
                started = time.monotonic()
                ranked = fn(item["q"])
                elapsed = (time.monotonic() - started) * 1000
                rank = 0
                for position, hit in enumerate(ranked[:10], start=1):
                    if documents.get(hit.get("document_id")) != item["doc"]:
                        continue
                    if int(hit.get("ordinal", -1)) in item["gold_ordinals"]:
                        rank = position
                        break
                if rank == 1:
                    stats["top1"] += 1
                if rank and rank <= 3:
                    stats["top3"] += 1
                if rank and rank <= 5:
                    stats["top5"] += 1
                if rank:
                    stats["rr"] += 1.0 / rank
                else:
                    tops = [(documents.get(k.get("document_id"), "?"), k.get("ordinal")) for k in ranked[:2]]
                    stats["misses"].append(f"{item['id']} (top={tops})")
                stats.setdefault("ms", []).append(elapsed)
            results[name] = stats

        print(f"{'mode':16} {'hit@1':>7} {'hit@3':>7} {'hit@5':>7} {'MRR':>7} {'ms/query':>9}")
        n = len(QUESTIONS)
        for name, s in results.items():
            print(
                f"{name:16} {s['top1']}/{n} ({s['top1']*100//n:2d}%) {s['top3']}/{n} "
                f"({s['top3']*100//n:2d}%) {s['top5']}/{n} ({s['top5']*100//n:2d}%) "
                f"{s['rr']/n:.3f}   {sum(s['ms'])/len(s['ms']):.0f}"
            )
        print()
        for name, s in results.items():
            if s["misses"]:
                print(f"{name} misses:")
                for m in s["misses"]:
                    print("  ", m)
                print()

        # False-confidence on questions the corpus cannot answer.
        print("top-1 final score per question (hybrid+rerank):")
        for item in QUESTIONS:
            ranked = modes["hybrid+rerank"](item["q"])
            top = ranked[0] if ranked else None
            label = "HIT " if top and documents.get(top.get("document_id")) == item["doc"] and int(top.get("ordinal", -1)) in item["gold_ordinals"] else "MISS"
            print(f"  {label} {item['id']:<22} {round(float(top.get('score') or 0), 4) if top else None}")
        print()
        print("separation of top-1 scores, positives vs unanswerable:")
        pos_scores = []
        for item in QUESTIONS:
            ranked = modes["hybrid+div+rerank"](item["q"])
            if ranked:
                pos_scores.append((item["id"], round(float(ranked[0].get("score") or 0), 4)))
        neg_scores = []
        for item in NEGATIVES:
            ranked = modes["hybrid+div+rerank"](item["q"])
            if ranked:
                neg_scores.append((item["id"], round(float(ranked[0].get("score") or 0), 4)))
        print("  positives :", pos_scores)
        print("  negatives :", neg_scores)
        if pos_scores and neg_scores:
            pmin = min(v for _, v in pos_scores)
            nmax = max(v for _, v in neg_scores)
            print(f"  min positive {pmin} vs max negative {nmax} -> "
                  f"{'SEPARABLE' if pmin > nmax else 'OVERLAPPING'}")
        print()
        print("unanswerable questions (top score returned by hybrid+rerank):")
        for item in NEGATIVES:
            ranked = modes["hybrid+rerank"](item["q"])
            top = ranked[0] if ranked else None
            print(
                f"  {item['id']}: returned={len(ranked)} "
                f"top={'<none>' if not top else (documents.get(top.get('document_id'), '?'), top.get('ordinal'), round(float(top.get('score') or 0), 4))}"
            )
        return 0
    finally:
        for proc in (embed_proc, rerank_proc):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        print("\nservers stopped")


if __name__ == "__main__":
    raise SystemExit(main())
