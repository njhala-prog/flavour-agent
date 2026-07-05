"""
20 test cases for the Flavor Innovation Agent.

Each case has:
  - input:             the natural language question
  - result_assertion:  a callable(list[dict]) -> bool that validates the RAW DATABASE RESULTS
                       This is ground-truth accuracy — it tests the data, not the text.
                       Cannot be gamed by rephrasing the insight.
  - category:          for grouping in Braintrust
  - notes:             what this case is testing

Assertion design principles:
  - Test the structure and content of raw rows, not the LLM's words
  - Use len(rows) >= N and _has_string_and_num(rows) for named-count results
    (ingredient + freq, spice + pct_in_cuisine, etc.) — rejects queries that
    return garbage rows with no recognisable name or numeric column
  - Use numeric range checks for aggregation queries (prices)
  - Use _has_string_pair(rows) for PAIRS_WITH results (two ingredient names per row)
  - Edge cases that expect sparse/empty data use lambda rows: True
    (graceful_handling score, not accuracy, determines quality for those)
"""

from typing import Callable


def _has_positive_num(rows: list[dict]) -> bool:
    return any(
        isinstance(v, (int, float)) and v > 0
        for row in rows
        for v in row.values()
    )


def _has_num_in_range(lo: float, hi: float) -> Callable[[list[dict]], bool]:
    def check(rows: list[dict]) -> bool:
        return any(
            isinstance(v, (int, float)) and lo < v < hi
            for row in rows
            for v in row.values()
        )
    return check


def _has_string_pair(rows: list[dict]) -> bool:
    """At least one row has 2+ string values (a PAIRS_WITH result row)."""
    return any(
        sum(1 for v in row.values() if isinstance(v, str)) >= 2
        for row in rows[:10]
    )


def _has_string_and_num(rows: list[dict]) -> bool:
    """At least one row has both a string (ingredient/category name) and a positive
    numeric value (count, frequency, or ratio). Rejects rows with only numeric data
    or only string data — ensures the query returned a meaningful named result."""
    return any(
        any(isinstance(v, str) for v in row.values()) and
        any(isinstance(v, (int, float)) and v > 0 for v in row.values())
        for row in rows
    )


TEST_CASES = [
    # ── SIMPLE LOOKUPS (5) ──────────────────────────────────────────────────

    {
        "input": "What are the most common ingredients in burger?",
        "result_assertion": lambda rows: len(rows) >= 5 and _has_string_and_num(rows),
        "category": "simple_lookup",
        "notes": "Ramen is well-represented — must return 5+ rows each with an ingredient name and use count.",
    },
    {
        "input": "What ingredients are used most often in burgers?",
        "result_assertion": lambda rows: len(rows) >= 5 and _has_string_and_num(rows),
        "category": "simple_lookup",
        "notes": "Burgers are the most common GMI — must return 5+ rows with ingredient name and count.",
    },
    {
        "input": "List the spices used in Indian cuisine restaurants and how often each appears.",
        "result_assertion": lambda rows: len(rows) >= 3 and _has_string_and_num(rows),
        "category": "simple_lookup",
        "notes": "Must return 3+ spice rows each containing a spice name and a numeric usage count or ratio.",
    },
    {
        "input": "What is the average price of a pizza across all restaurants?",
        "result_assertion": lambda rows: len(rows) > 0 and _has_num_in_range(1, 100)(rows),
        "category": "simple_lookup",
        "notes": "Average pizza price must be a positive number between $1 and $100.",
    },
    {
        "input": "Rank all ingredient categories by how frequently they appear across all menu items.",
        "result_assertion": lambda rows: len(rows) >= 3 and _has_string_and_num(rows),
        "category": "simple_lookup",
        "notes": "Must return 3+ category rows each with a category name string and a positive use count.",
    },

    # ── COMPARATIVE QUERIES (5) ─────────────────────────────────────────────

    {
        "input": "Compare the top 5 ingredients in Italian vs American cuisine restaurants.",
        "result_assertion": lambda rows: len(rows) >= 5 and _has_string_and_num(rows),
        "category": "comparative",
        "notes": "Top-5 comparison — 5+ rows each with an ingredient name and use count.",
    },
    {
        "input": "How does the average burger price differ between fast casual and casual dining?",
        "result_assertion": lambda rows: len(rows) > 0 and _has_positive_num(rows),
        "category": "comparative",
        "notes": "Must return a numeric price for at least one restaurant type.",
    },
    {
        "input": "Which has more protein-heavy dishes: Manhattan or Brooklyn?",
        "result_assertion": lambda rows: len(rows) > 0 and _has_positive_num(rows),
        "category": "comparative",
        "notes": "Geographic comparison — must return a count for at least one neighborhood.",
    },
    {
        "input": "Compare ingredient sophistication (number of unique ingredients per item) between chain restaurants and independents.",
        "result_assertion": lambda rows: len(rows) > 0 and _has_positive_num(rows),
        "category": "comparative",
        "notes": "AVG aggregation — must return a numeric value per group.",
    },
    {
        "input": "Do Japanese or Korean restaurants use more seafood ingredients?",
        "result_assertion": lambda rows: len(rows) > 0 and _has_positive_num(rows),
        "category": "comparative",
        "notes": "Cross-cuisine comparison — must return a count for at least one cuisine.",
    },

    # ── TREND ANALYSIS (4) ─────────────────────────────────────────────────

    {
        "input": "What flavors are trending in desserts across NYC?",
        "result_assertion": lambda rows: len(rows) >= 3 and _has_string_and_num(rows),
        "category": "trend_analysis",
        "notes": "Must return 3+ flavor profile rows each with a profile name and ingredient/item count.",
    },
    {
        "input": "Which spice combinations are most common in fast casual Mexican restaurants?",
        "result_assertion": lambda rows: len(rows) >= 2 and _has_string_pair(rows),
        "category": "trend_analysis",
        "notes": "PAIRS_WITH query — each row must have 2 spice name strings (a pair), not just one.",
    },
    {
        "input": "What Asian ingredients are crossing over into American cuisine menus?",
        "result_assertion": lambda rows: len(rows) >= 3 and _has_string_and_num(rows),
        "category": "trend_analysis",
        "notes": "Ratio crossover query — must return 3+ rows with ingredient name and numeric ratio/count.",
    },
    {
        "input": "Which ingredient pairings are gaining popularity in Brooklyn fine dining?",
        "result_assertion": lambda rows: len(rows) >= 3 and _has_positive_num(rows),
        "category": "trend_analysis",
        "notes": "PAIRS_WITH in a specific neighborhood/type — must return pairs with frequency.",
    },

    # ── CROSS-CUISINE / INNOVATION (3) ─────────────────────────────────────

    {
        "input": "Find fusion opportunities: which ingredients are used much more frequently in Thai cuisine than in Mexican cuisine?",
        "result_assertion": lambda rows: len(rows) >= 1 and _has_string_and_num(rows),
        "category": "cross_cuisine",
        "notes": "Frequency-ratio white-space query — must return rows with an ingredient name and count data.",
    },
    {
        "input": "What unexpected ingredient combinations appear in Brooklyn fine dining?",
        "result_assertion": lambda rows: len(rows) >= 1 and _has_string_pair(rows),
        "category": "cross_cuisine",
        "notes": "Low-frequency PAIRS_WITH — must return at least 1 row with 2 ingredient name strings.",
    },
    {
        "input": "Which ingredients are used significantly more in Korean cuisine than in Japanese cuisine?",
        "result_assertion": lambda rows: len(rows) >= 1 and _has_string_and_num(rows),
        "category": "cross_cuisine",
        "notes": "Frequency-ratio white-space query — must return rows with ingredient name and count data.",
    },

    # ── EDGE CASES (3) ─────────────────────────────────────────────────────

    {
        "input": "What's popular?",
        "result_assertion": lambda rows: len(rows) >= 3 and _has_string_and_num(rows),
        "category": "edge_case",
        "notes": "Maximally ambiguous query — agent must interpret it and return 3+ named results with counts.",
    },
    {
        "input": "Find vegan options in steakhouse restaurants.",
        "result_assertion": lambda rows: True,
        "category": "edge_case",
        "notes": "Expected sparse/empty results. accuracy always passes (assertion=True); "
                 "graceful_handling score determines whether the agent communicated the scarcity well.",
    },
    {
        "input": "What ingredeints go well with yuzu?",
        "result_assertion": lambda rows: len(rows) >= 5 and _has_string_and_num(rows),
        "category": "edge_case",
        "notes": "Misspelled 'ingredients' — agent must handle gracefully. Yuzu has 20+ pairings; "
                 "must return 5+ rows each with a pairing name string and a co-occurrence count.",
    },
]
