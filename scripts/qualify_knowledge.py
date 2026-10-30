"""Explicit pinned CPU qualification, separate from database/RAG acceptance."""

import argparse
import json
import time
from pathlib import Path

from knowledge.embedding import EmbeddingUnavailable, validate_vectors
from knowledge.profile import PROFILE


def evaluate(corpus, embed):
    """Score held-out questions at the fixed 0.55 gate, without changing the corpus."""
    documents = [(d["id"], validate_vectors([embed(d["text"])])[0]) for d in corpus["documents"]]
    cases = []
    for query in corpus["queries"]:
        started = time.monotonic()
        vector = validate_vectors([embed(query["text"])])[0]
        ranked = sorted(
            ((sum(a * b for a, b in zip(vector, other)), key) for key, other in documents),
            reverse=True,
        )
        cosine, best = ranked[0]
        matched = best if cosine >= 0.55 else None
        cases.append(
            {
                "id": query["id"],
                "direction": query["direction"],
                "expected": query["expected"],
                "matched": matched,
                "cosine": round(cosine, 6),
                "seconds": round(time.monotonic() - started, 3),
                "passed": matched == query["expected"],
            }
        )
    return {"passed": bool(cases) and all(c["passed"] for c in cases), "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-real-model", action="store_true")
    parser.add_argument("--assets", type=Path, default=Path(".local/knowledge-model"))
    args = parser.parse_args()
    if not args.allow_real_model:
        print(json.dumps({"status": "opt_in_required"}))
        return 2
    # Import/start only after the explicit opt-in. No dotenv, download or GPU fallback.
    from embedding_service.supervisor import Supervisor

    corpus = json.loads(
        (
            Path(__file__).resolve().parents[1] / "evaluation/fixtures/knowledge-qualification.json"
        ).read_text()
    )
    worker = Supervisor(args.assets)
    report = {
        "fingerprint": PROFILE.fingerprint,
        "kind": "dense-cpu-qualification",
        "threshold": 0.55,
        "end_to_end_rag": False,
    }
    started = time.monotonic()
    try:
        worker.start()
        report["startup_seconds"] = round(time.monotonic() - started, 3)
        report.update(
            evaluate(corpus, lambda text: worker.embed([text], deadline=time.monotonic() + 10)[0])
        )
        report["status"] = "passed" if report["passed"] else "failed"
        return 0 if report["passed"] else 1
    except EmbeddingUnavailable as exc:
        report.update(status="unavailable", reason=str(exc))
        return 1
    finally:
        report["peak_child_rss_mib"] = round(worker.peak_rss / 1024**2, 2)
        worker.close()
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
