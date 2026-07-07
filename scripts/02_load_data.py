"""
Load all CSV data into Neo4j using batched UNWIND queries.

Speed optimizations vs naive approach:
  - BATCH_SIZE=2000 (was 500) — fewer round-trips to Neo4j
  - Three-phase parallel loading via ThreadPoolExecutor:
      Phase 1 (parallel): ingredients, GMIs, restaurants  (no dependencies)
      Phase 2 (parallel): menu_items, flavor_profiles     (need phase 1)
      Phase 3:            item_ingredients                (needs phase 2)

Run: python scripts/02_load_data.py
"""

import os
import sys
import pandas as pd
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

DATA_DIR   = Path(__file__).parent.parent
BATCH_SIZE = 5000      # rows per Neo4j round-trip
N_WORKERS  = 4         # parallel sessions for item_ingredients

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")


def apply_schema(driver):
    """Apply constraints and indexes from 01_schema.cypher before loading data."""
    schema_path = Path(__file__).parent / "01_schema.cypher"
    raw = schema_path.read_text()
    # Strip comment lines then split on semicolons
    lines = [l for l in raw.splitlines() if not l.strip().startswith("//")]
    statements = [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
    with driver.session() as session:
        for stmt in statements:
            session.run(stmt)
    print(f"  Applied {len(statements)} constraints/indexes from 01_schema.cypher")


def run_batched(driver, query: str, rows: list[dict], label: str):
    """Open a dedicated session and load rows in batches of BATCH_SIZE."""
    with driver.session() as session:
        for i in tqdm(range(0, len(rows), BATCH_SIZE), desc=label):
            batch = rows[i : i + BATCH_SIZE]
            session.run(query, rows=batch)


def load_restaurants(driver):
    df = pd.read_csv(DATA_DIR / "restaurants.csv").fillna("")
    rows = df.to_dict("records")
    run_batched(
        driver,
        """
        UNWIND $rows AS row
        MERGE (r:Restaurant {id: toInteger(row.restaurant_id)})
        SET r.name       = row.business_name,
            r.city       = row.city,
            r.state      = row.state,
            r.type       = row.restaurant_type,
            r.chain_name = row.chain_name

        MERGE (c:Cuisine {name: coalesce(row.cuisine, 'Unknown')})
        MERGE (r)-[:HAS_CUISINE]->(c)

        MERGE (n:Neighborhood {name: coalesce(row.city, 'Unknown')})
        MERGE (r)-[:LOCATED_IN]->(n)

        WITH r, row
        WHERE row.chain_name <> ''
        MERGE (ch:Chain {name: row.chain_name})
        MERGE (r)-[:PART_OF_CHAIN]->(ch)
        """,
        rows,
        "Restaurants",
    )
    print(f"  [restaurants] {len(rows):,} rows done")


def load_ingredients(driver):
    df = pd.read_csv(DATA_DIR / "ingredients_retagged.csv").fillna("")
    rows = df.to_dict("records")
    run_batched(
        driver,
        """
        UNWIND $rows AS row
        MERGE (i:Ingredient {id: toInteger(row.ingredient_id)})
        SET i.name     = row.ingredient_name,
            i.category = row.category

        MERGE (ic:IngredientCategory {name: coalesce(row.category, 'other')})
        MERGE (i)-[:IN_CATEGORY]->(ic)
        """,
        rows,
        "Ingredients",
    )
    print(f"  [ingredients] {len(rows):,} rows done")


def load_gmis(driver):
    df = pd.read_csv(DATA_DIR / "gmis.csv").fillna("")
    rows = df.to_dict("records")
    run_batched(
        driver,
        """
        UNWIND $rows AS row
        MERGE (g:GMI {id: toInteger(row.gmi_id)})
        SET g.name     = row.gmi_name,
            g.category = row.category
        """,
        rows,
        "GMIs",
    )
    print(f"  [gmis] {len(rows):,} rows done")


def load_menu_items(driver):
    df = pd.read_csv(DATA_DIR / "menu_items_retagged.csv", encoding="utf-8", errors="replace").fillna("")
    rows = df.to_dict("records")

    def price_tier(price):
        try:
            p = float(price)
            return "budget" if p < 10 else ("mid" if p <= 25 else "premium")
        except (ValueError, TypeError):
            return "unknown"

    for row in rows:
        row["price_tier"] = price_tier(row.get("price", ""))
        try:
            row["price"] = float(row["price"])
        except (ValueError, TypeError):
            row["price"] = 0.0

    missing = sum(1 for r in rows if not r.get("gmi_name", "").strip())
    if missing:
        print(f"  WARNING: {missing} menu items have no gmi_name — IS_TYPE skipped for these")

    run_batched(
        driver,
        """
        UNWIND $rows AS row
        MERGE (m:MenuItem {id: toInteger(row.item_id)})
        SET m.name        = row.menu_item_name,
            m.description = row.description,
            m.price       = toFloat(row.price),
            m.price_tier  = row.price_tier

        WITH m, row
        MATCH (r:Restaurant {id: toInteger(row.restaurant_id)})
        MERGE (r)-[:SERVES]->(m)

        WITH m, row
        OPTIONAL MATCH (g:GMI {name: row.gmi_name})
        FOREACH (_ IN CASE WHEN g IS NOT NULL THEN [1] ELSE [] END |
            MERGE (m)-[:IS_TYPE]->(g)
        )

        WITH m, row
        MERGE (pt:PriceTier {name: row.price_tier})
        MERGE (m)-[:IN_PRICE_TIER]->(pt)
        """,
        rows,
        "Menu Items",
    )
    print(f"  [menu_items] {len(rows):,} rows done")


FLAVOR_PROFILE_MAP = {
    "spicy": [
        "chili powder", "sriracha", "jalapeño", "cayenne", "black pepper",
        "ginger", "horseradish", "wasabi", "paprika", "chipotle",
        "red pepper", "chili", "hot sauce", "curry powder", "harissa",
        "pepper", "turmeric", "chili flakes", "szechuan pepper",
    ],
    "sweet": [
        "sugar", "honey", "maple syrup", "mango", "pineapple", "apple",
        "coconut", "caramel", "chocolate", "vanilla", "brown sugar",
        "banana", "strawberry", "blueberry", "peach", "cherry",
        "orange", "cinnamon", "nutmeg", "condensed milk", "agave",
        "sweet potato", "dates", "raisin", "fig",
    ],
    "savory": [
        "beef", "chicken", "pork", "lamb", "bacon", "butter", "cheese",
        "garlic", "onion", "olive oil", "sausage", "ham", "duck",
        "turkey", "ground beef", "steak", "pulled pork", "brisket",
        "cheddar", "mozzarella", "feta", "cream cheese", "scallions",
        "shallot", "leek", "bread", "tortilla", "rice", "pasta",
        "ramen noodles", "udon noodles", "potato", "mayo", "mustard",
        "ranch", "garlic powder", "onion powder", "prosciutto", "chorizo",
        "salami", "pepperoni", "anchovies", "egg", "eggs",
    ],
    "umami": [
        "soy sauce", "miso", "mushroom", "parmesan", "tomato", "beef",
        "tuna", "salmon", "shrimp", "crab", "scallops", "fish sauce",
        "oyster sauce", "worcestershire", "truffle", "seaweed", "nori",
        "dashi", "kimchi", "blue cheese", "tempeh", "anchovies",
        "bonito", "scallion", "miso paste", "black bean",
    ],
    "sour": [
        "lemon", "lime", "vinegar", "sour cream", "yogurt", "citrus",
        "yuzu", "tamarind", "pickle", "tomato", "orange", "grapefruit",
        "buttermilk", "balsamic", "apple cider vinegar", "kimchi",
    ],
    "smoky": [
        "bacon", "chipotle", "smoked paprika", "pulled pork", "brisket",
        "bbq sauce", "liquid smoke", "chorizo", "paprika", "charcoal",
    ],
    "herbal": [
        "basil", "cilantro", "parsley", "thyme", "rosemary", "oregano",
        "mint", "dill", "sage", "tarragon", "chives", "bay leaf",
        "lemongrass", "za'atar", "coriander", "fennel", "marjoram",
        "lavender", "scallions",
    ],
}


CATEGORY_FLAVOR_MAP = {
    "sweetener":               "sweet",
    "fruit":                   "sweet",
    "dairy":                   "savory",
    "herbs/spices":            "herbal",
    "spice":                   "spicy",
    "meat":                    "savory",
    "seafood":                 "umami",
    "fats/oils":               "savory",
    "nuts/seeds/grains":       "savory",
    "grains":                  "savory",
    "vegetable":               "savory",
}


def load_flavor_profiles(driver):
    # Phase A — name-based matching (expanded list)
    name_rows = [
        {"profile": profile, "ingredient": name}
        for profile, names in FLAVOR_PROFILE_MAP.items()
        for name in names
    ]
    # Phase B — category-based fallback for retagged ingredients
    cat_rows = [
        {"profile": fp, "category": cat}
        for cat, fp in CATEGORY_FLAVOR_MAP.items()
    ]
    with driver.session() as session:
        # Create FlavorProfile nodes
        session.run(
            "UNWIND $profiles AS p MERGE (:FlavorProfile {name: p})",
            profiles=list(FLAVOR_PROFILE_MAP.keys()),
        )
        # Name-based links
        session.run(
            """
            UNWIND $rows AS row
            MERGE (fp:FlavorProfile {name: row.profile})
            WITH fp, row
            OPTIONAL MATCH (i:Ingredient {name: row.ingredient})
            FOREACH (_ IN CASE WHEN i IS NOT NULL THEN [1] ELSE [] END |
                MERGE (i)-[:HAS_FLAVOR]->(fp)
            )
            """,
            rows=name_rows,
        )
        # Category-based links (only for ingredients not yet tagged)
        session.run(
            """
            UNWIND $rows AS row
            MATCH (i:Ingredient {category: row.category})
            WHERE NOT (i)-[:HAS_FLAVOR]->()
            MATCH (fp:FlavorProfile {name: row.profile})
            MERGE (i)-[:HAS_FLAVOR]->(fp)
            """,
            rows=cat_rows,
        )
        total = session.run(
            "MATCH ()-[:HAS_FLAVOR]->() RETURN count(*) AS n"
        ).single()["n"]
    print(f"  [flavor_profiles] {total} ingredient->FlavorProfile links done")


def load_item_ingredients(driver):
    """
    Sequential load with large batches (5000 rows each = 31 trips for 151k rows).
    Parallel sessions deadlock on concurrent MERGE writes to shared nodes,
    so sequential is used here. The bigger BATCH_SIZE still gives a large speedup
    over the original 500-row batches (303 trips -> 31 trips).
    """
    df = pd.read_csv(DATA_DIR / "item_ingredients_retagged.csv").fillna("")
    rows = df.to_dict("records")
    run_batched(
        driver,
        """
        UNWIND $rows AS row
        MATCH (m:MenuItem   {id: toInteger(row.item_id)})
        MATCH (i:Ingredient {id: toInteger(row.ingredient_id)})
        MERGE (m)-[:CONTAINS]->(i)
        """,
        rows,
        "Item-Ingredient links",
    )
    print(f"  [item_ingredients] {len(rows):,} rows done")


def run_parallel(fns, driver):
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        futures = {ex.submit(fn, driver): fn.__name__ for fn in fns}
        for f in as_completed(futures):
            exc = f.exception()
            if exc:
                raise RuntimeError(f"{futures[f]} failed: {exc}") from exc


def main():
    print("Connecting to Neo4j...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        print("Connected.\n")
    except Exception as e:
        print(f"Cannot connect to Neo4j: {e}")
        sys.exit(1)

    print("Applying schema constraints and indexes...")
    apply_schema(driver)

    print("\nPhase 1 — loading independent tables in parallel (ingredients / GMIs / restaurants)...")
    run_parallel([load_ingredients, load_gmis, load_restaurants], driver)

    print("\nPhase 2 — loading menu items + flavor profiles in parallel...")
    run_parallel([load_menu_items, load_flavor_profiles], driver)

    print("\nPhase 3 — loading item-ingredient links...")
    load_item_ingredients(driver)

    driver.close()
    print("\nAll data loaded successfully.")
    print("Next: run scripts/03_compute_pairs.py to build PAIRS_WITH relationships.")


if __name__ == "__main__":
    main()
