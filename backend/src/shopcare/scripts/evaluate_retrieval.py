"""Run fixed local retrieval smoke cases without touching the demo database."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shopcare.db import build_engine, init_db
from shopcare.main import app, settings
from shopcare.services.policy_chunker import split_policy
from shopcare.services.policy_loader import load_policies
from shopcare.services.policy_store import replace_policy_chunks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行本地词法检索小样本验证")
    parser.add_argument("--cases", default="data/eval/retrieval_baseline.json")
    parser.add_argument("--output", default="docs/retrieval-baseline.md")
    args = parser.parse_args(argv)
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    policies = load_policies(Path("data/policies"))
    rows = []
    with TemporaryDirectory(prefix="shopcare-retrieval-") as directory:
        url = f"sqlite:///{(Path(directory) / 'evaluation.db').as_posix()}"
        engine = build_engine(url)
        init_db(engine)
        try:
            with Session(engine) as session, session.begin():
                for policy in policies:
                    replace_policy_chunks(session, policy, split_policy(policy))
        finally:
            engine.dispose()
        with patch.object(settings, "database_url", url), TestClient(app) as client:
            for case in cases:
                response = client.get(
                    "/api/policies/search",
                    params={
                        "q": case["query"],
                        "category": case["category"],
                        "as_of": case["as_of"],
                        "top_k": 3,
                    },
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Evaluation case {case['id']} returned HTTP {response.status_code}"
                    )
                hits = response.json()
                expected = (case["expected_policy_id"], case["expected_version"])
                rank = next(
                    (
                        i + 1
                        for i, hit in enumerate(hits)
                        if (hit["chunk"]["policy_id"], hit["chunk"]["version"]) == expected
                    ),
                    None,
                )
                rows.append({"case": case, "rank": rank, "hits": hits})

    positive = [row for row in rows if row["case"]["expected_policy_id"] is not None]
    negative = [row for row in rows if row["case"]["expected_policy_id"] is None]
    top1 = sum(row["rank"] == 1 for row in positive)
    top3 = sum(row["rank"] is not None for row in positive)
    empty_ok = sum(not row["hits"] for row in negative)
    lines = [
        "# 词法检索初始基线",
        "",
        "八份自编政策、十个人工固定问题，通过实际 HTTP 路由在独立临时数据库运行。",
        "无模型、embedding 或真实商家数据；不同案例的参照日期见输入文件。",
        "",
        f"- 有目标政策问题：{len(positive)} 个。",
        f"- Hit@1：{top1}/{len(positive)}。",
        f"- Hit@3：{top3}/{len(positive)}。",
        f"- 预期无结果案例正确返回空列表：{empty_ok}/{len(negative)}。",
        "",
        "Hit@k 的分母仅为有目标政策的案例，匹配同时要求 policy_id 和 version。",
        "每个案例只标注一个目标政策，因此这里记录 Hit@k，不宣称完整相关性 Recall。",
        "",
        "| ID | 问题 | 目标政策 | 前三条（分数） | 目标位置 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        case = row["case"]
        target = (
            f"{case['expected_policy_id']}:{case['expected_version']}"
            if case["expected_policy_id"]
            else "应返回空列表"
        )
        actual = (
            ", ".join(
                f"{hit['chunk']['policy_id']}:{hit['chunk']['version']} ({hit['score']})"
                for hit in row["hits"]
            )
            or "空列表"
        )
        rank = str(row["rank"]) if row["rank"] is not None else "未命中 / 不适用"
        lines.append(f"| {case['id']} | {case['query']} | {target} | {actual} | {rank} |")
    lines += [
        "",
        "## 解释与局限",
        "",
        "- 本组案例是开发用小样本，其中多条用词接近政策原文，不是独立保留测试集。",
        "- 双字/英文特征只支持字面匹配，不能理解同义改写、否定或完整业务资格。",
        "- Q09 故意使用口语改写；结果应原样保留，不能修改预期来美化指标。",
        "- 高分只代表命中特征多，不是置信度；相似但不适用的问题仍可能返回片段。",
        "- 日期/类别正确属于程序约束，不等于最终售后结论正确。",
        "- 下一轮应补独立问题、歧义/否定与无答案案例，再比较检索方案。",
        "",
        "## 复现",
        "",
        "```powershell",
        ".venv/Scripts/python -m shopcare.scripts.evaluate_retrieval",
        "```",
        "",
    ]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "cases": len(rows),
                "positive_cases": len(positive),
                "hit_at_1": top1,
                "hit_at_3": top3,
                "empty_cases": len(negative),
                "empty_correct": empty_ok,
                "report": str(output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
