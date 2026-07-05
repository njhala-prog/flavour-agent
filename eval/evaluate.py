import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

"""
Braintrust evaluation suite for the Flavor Innovation Agent.

Scoring dimensions match the assignment specification:
  - Accuracy    (30%) -- does the answer correctly reflect the data? Are statistics accurate?
                         Tested via result_assertion(raw_results) — ground truth, not keywords.
                         Cannot be gamed by rephrasing the insight text.
  - Relevance   (25%) -- does the insight directly answer the question? (LLM judge)
  - Creativity  (25%) -- does the agent surface non-obvious insights? (LLM judge, standard cases)
    OR
  - Graceful Handling (25%) -- did the agent handle ambiguity/sparsity/typos well? (edge cases)
                               Edge cases test a different capability than standard insight quality,
                               so creativity is replaced with graceful handling for those queries.
  - Latency     (20%) -- response time scored linearly against 10s target (deterministic)

Composite = accuracy*0.30 + relevance*0.25 + creativity_or_gh*0.25 + latency*0.20

Run:
    python eval/evaluate.py
"""

import logging
import braintrust
from openai import OpenAI
from dotenv import load_dotenv

from eval.test_cases import TEST_CASES
from src.agent import FlavorInnovationAgent

load_dotenv()
logging.basicConfig(level=logging.WARNING)

BRAINTRUST_PROJECT = "menudata-flavor-agent"
LATENCY_TARGET_SECONDS = 10.0

_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
JUDGE_MODEL = "gpt-4o-mini"


def _llm_judge(system_prompt: str, user_content: str) -> str:
    resp = _openai.chat.completions.create(
        model=JUDGE_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return resp.choices[0].message.content.strip().upper()


# ── Scorer: Accuracy (30%) ────────────────────────────────────────────────────

def score_accuracy(raw_results: list, case: dict) -> float:
    """
    Ground-truth accuracy: run the result_assertion callable from the test case
    against the actual raw database rows. Tests whether the Cypher query returned
    the RIGHT data — correct shape, correct counts, correct statistics.

    This cannot be gamed by rephrasing the insight text. The assertion is a
    verifiable property of the data itself (e.g., "at least 3 rows", "contains
    a numeric value", "each row has two ingredient name fields").
    """
    assertion = case.get("result_assertion")
    if assertion is None:
        return 1.0 if raw_results else 0.0
    try:
        return 1.0 if assertion(raw_results) else 0.0
    except Exception as exc:
        logging.warning("result_assertion raised: %s", exc)
        return 0.0


# ── Scorer: Relevance (25%) ───────────────────────────────────────────────────

def score_relevance(question: str, output: str) -> float:
    verdict = _llm_judge(
        (
            "Score whether the answer directly addresses the question.\n"
            "Reply with exactly one word: YES, PARTIAL, or NO.\n"
            "YES = on-topic and complete. PARTIAL = related but incomplete. NO = off-topic."
        ),
        f"Question: {question}\nAnswer: {output}",
    )
    if "YES" in verdict:
        return 1.0
    if "PARTIAL" in verdict:
        return 0.5
    return 0.0


# ── Scorer: Creativity (25%, standard cases) ──────────────────────────────────

def score_creativity(question: str, output: str) -> float:
    verdict = _llm_judge(
        (
            "Does the answer surface non-obvious insights or unexpected connections beyond a plain list?\n"
            "Reply with exactly one word: HIGH, MEDIUM, or LOW.\n"
            "HIGH = surprising or novel finding backed by data. "
            "MEDIUM = useful but expected. LOW = just a plain list."
        ),
        f"Question: {question}\nAnswer: {output}",
    )
    if "HIGH" in verdict:
        return 1.0
    if "MEDIUM" in verdict:
        return 0.5
    return 0.0


# ── Scorer: Graceful Handling (25%, edge cases only) ──────────────────────────

def score_graceful_handling(question: str, output: str) -> float:
    """
    For edge cases: did the agent handle ambiguity, sparse data, or typos well?
    Replaces creativity for queries where insight novelty is the wrong measure —
    a vegan-in-steakhouse answer should not be penalised for lacking surprise.
    """
    verdict = _llm_judge(
        (
            "Evaluate how well the response handles a difficult query "
            "(ambiguous phrasing, very sparse data, or a misspelling).\n"
            "Consider: Does it acknowledge uncertainty? Does it try a reasonable "
            "interpretation? Does it describe what was or wasn't found honestly?\n"
            "Reply with exactly one word: HIGH, MEDIUM, or LOW."
        ),
        f"Question: {question}\nAnswer: {output}",
    )
    if "HIGH" in verdict:
        return 1.0
    if "MEDIUM" in verdict:
        return 0.5
    return 0.0


# ── Scorer: Latency (20%) ─────────────────────────────────────────────────────

def score_latency(latency_seconds: float) -> float:
    return round(max(0.0, 1.0 - (latency_seconds / LATENCY_TARGET_SECONDS)), 3)


# ── Main Eval Loop ─────────────────────────────────────────────────────────────

def _print_separator(char="-", width=80):
    print(char * width)


def _print_summary(results: list[dict]):
    _print_separator("=", 80)
    print("EVALUATION SUMMARY")
    _print_separator("=", 80)
    header = f"{'#':>2}  {'Category':<16} {'Question':<42} {'acc':>4} {'rel':>4} {'cr/gh':>5} {'lat':>4} {'TOTAL':>6}"
    print(header)
    _print_separator()

    for r in results:
        third = r["gh"] if r["is_edge"] else r["cr"]
        label = "gh" if r["is_edge"] else "cr"
        q = r["question"][:41] + "…" if len(r["question"]) > 42 else r["question"]
        print(
            f"{r['idx']:>2}.  {r['category']:<16} {q:<42} "
            f"{r['acc']:>4.2f} {r['rel']:>4.2f} {third:>5.2f} {r['lat']:>4.2f} {r['composite']:>6.3f}"
        )

    _print_separator()
    avg = lambda key: sum(r[key] for r in results) / len(results)
    avg_third = sum(
        r["gh"] if r["is_edge"] else r["cr"] for r in results
    ) / len(results)
    print(
        f"AVG  acc={avg('acc'):.2f} | rel={avg('rel'):.2f} | "
        f"cr/gh={avg_third:.2f} | lat={avg('lat'):.2f} | composite={avg('composite'):.3f}"
    )
    _print_separator("=")


def run_eval():
    braintrust.login(api_key=os.getenv("BRAINTRUST_API_KEY"))
    experiment = braintrust.init(project=BRAINTRUST_PROJECT)

    results = []

    with FlavorInnovationAgent() as agent:
        for idx, case in enumerate(TEST_CASES, start=1):
            question = case["input"]
            is_edge = case["category"] == "edge_case"

            _print_separator()
            print(f"[{idx:02d}/{len(TEST_CASES)}] {question}")
            _print_separator()

            try:
                response = agent.query(question)
                output = response.insight
                latency = response.latency_seconds
                raw_results = response.raw_results
                error = None
                print(f"Cypher : {response.cypher[:120]}{'...' if len(response.cypher) > 120 else ''}")
                print(f"Rows   : {len(raw_results)} | Latency: {latency:.2f}s | Retries: {response.retries}")
                print(f"\nAnswer : {output}\n")
            except Exception as e:
                output = f"ERROR: {e}"
                latency = LATENCY_TARGET_SECONDS
                raw_results = []
                error = str(e)
                response = None
                print(f"ERROR  : {e}\n")

            acc = score_accuracy(raw_results, case)
            rel = score_relevance(question, output)
            lat = score_latency(latency)

            if is_edge:
                gh = score_graceful_handling(question, output)
                cr = 0.0
                scores = {
                    "accuracy":          acc,
                    "relevance":         rel,
                    "graceful_handling": gh,
                    "creativity":        None,
                    "latency":           lat,
                }
                composite = round(acc * 0.30 + rel * 0.25 + gh * 0.25 + lat * 0.20, 3)
                print(f"Scores : acc={acc:.2f} | rel={rel:.2f} | gh={gh:.2f} | lat={lat:.2f} | composite={composite:.3f}")
            else:
                cr = score_creativity(question, output)
                gh = 0.0
                scores = {
                    "accuracy":          acc,
                    "relevance":         rel,
                    "creativity":        cr,
                    "graceful_handling": None,
                    "latency":           lat,
                }
                composite = round(acc * 0.30 + rel * 0.25 + cr * 0.25 + lat * 0.20, 3)
                print(f"Scores : acc={acc:.2f} | rel={rel:.2f} | cr={cr:.2f} | lat={lat:.2f} | composite={composite:.3f}")

            experiment.log(
                input=question,
                output=output,
                expected={"assertion": case.get("notes", "")},
                scores={k: v for k, v in {**scores, "composite": composite}.items() if v is not None},
                metadata={
                    "category":        case["category"],
                    "notes":           case.get("notes", ""),
                    "cypher":          getattr(response, "cypher", ""),
                    "retries":         getattr(response, "retries", 0),
                    "raw_rows":        len(raw_results),
                    "latency_seconds": latency,
                    "error":           error,
                },
            )

            results.append({
                "idx":      idx,
                "category": case["category"],
                "question": question,
                "acc":      acc,
                "rel":      rel,
                "cr":       cr,
                "gh":       gh,
                "lat":      lat,
                "composite": composite,
                "is_edge":  is_edge,
            })

    _print_summary(results)
    print(f"\nFull results: https://www.braintrust.dev/app")


if __name__ == "__main__":
    run_eval()
