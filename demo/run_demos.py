"""
5 showcase demo queries that highlight the agent's capabilities.

Run: python demo/run_demos.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.agent import FlavorInnovationAgent

DEMO_QUERIES = [
    {
        "title": "1. Cross-Cuisine Crossover",
        "question": "What Asian spices and ingredients are crossing over into American cuisine menus in NYC?",
    },
    {
        "title": "2. White-Space Innovation",
        "question": "Find ingredients that appear much more frequently in Japanese cuisine than in Korean cuisine - ingredients where Japanese restaurants use them at least 3x more than Korean restaurants.",
    },
    {
        "title": "3. Unexpected Pairings",
        "question": "What unusual ingredient combinations appear in Brooklyn fine dining that you wouldn't expect to see together?",
    },
    {
        "title": "4. Emerging Trend",
        "question": "Which spice combinations are trending in fast casual restaurants - find pairs that appear frequently but only in newer or niche cuisines?",
    },
    {
        "title": "5. Flavor Profile Gap",
        "question": "What are the top 10 most common ingredient pairings in Italian cuisine restaurants, ranked by how often they appear together?",
    },
]


def main():
    print("=" * 65)
    print("  MenuData Flavor Innovation Agent - Demo Queries")
    print("=" * 65)

    with FlavorInnovationAgent() as agent:
        for demo in DEMO_QUERIES:
            print(f"\n{'-' * 65}")
            print(f"  {demo['title']}")
            print(f"{'-' * 65}")
            print(f"  Q: {demo['question']}\n")

            try:
                response = agent.query(demo["question"])
            except Exception as e:
                print(f"  ERROR: {e}")
                continue

            print(f"  CYPHER:\n  {response.cypher}\n")
            print(f"  RAW RESULTS ({len(response.raw_results)} rows):")
            for row in response.raw_results[:5]:
                print(f"    {row}")
            if len(response.raw_results) > 5:
                print(f"    ... and {len(response.raw_results) - 5} more rows")

            print(f"\n  INSIGHT:\n  {response.insight}")
            print(f"\n  Latency: {response.latency_seconds}s | Retries: {response.retries}")

    print(f"\n{'=' * 65}")
    print("  Done.")
    print("=" * 65)



if __name__ == "__main__":
    main()
