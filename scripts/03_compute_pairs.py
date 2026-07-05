"""
Compute PAIRS_WITH co-occurrence relationships between ingredients.

Two ingredients PAIRS_WITH each other if they appear together in >= MIN_FREQUENCY
menu items. Each edge also stores which cuisines the pairing appears in.

Speed approach:
  apoc.periodic.iterate runs the heavy MATCH+MERGE entirely server-side with no
  Python round-trips per batch. Much faster than the Python-loop approach for
  large graphs. APOC is already enabled via NEO4J_PLUGINS in docker-compose.yml.

Run AFTER 02_load_data.py:
    python scripts/03_compute_pairs.py
"""

import os
import sys
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

MIN_FREQUENCY = 5
APOC_BATCH    = 500


def compute_pairs(session):
    print("Computing ingredient co-occurrence pairs (server-side via APOC)...")
    print(f"  Min frequency threshold: {MIN_FREQUENCY}")

    result = session.run(
        """
        CALL apoc.periodic.iterate(
            "MATCH (i1:Ingredient)<-[:CONTAINS]-(:MenuItem)-[:CONTAINS]->(i2:Ingredient)
             WHERE i1.id < i2.id
             WITH i1, i2, count(*) AS freq
             WHERE freq >= $min_freq
             RETURN i1, i2, freq",
            "MERGE (i1)-[r:PAIRS_WITH]-(i2)
             SET r.frequency = freq",
            {batchSize: $batch, iterateList: false, params: {min_freq: $min_freq}}
        )
        YIELD batches, total, timeTaken, committedOperations
        RETURN batches, total, timeTaken, committedOperations
        """,
        min_freq=MIN_FREQUENCY,
        batch=APOC_BATCH,
    )
    row = result.single()
    print(f"  Created {row['committedOperations']:,} PAIRS_WITH relationships "
          f"in {row['batches']} batches ({row['timeTaken']:.1f}s server-side)")


def tag_cuisine_pairs(session):
    print("Tagging pairs with cuisine context (server-side via APOC)...")

    result = session.run(
        """
        CALL apoc.periodic.iterate(
            "MATCH (i1:Ingredient)<-[:CONTAINS]-(m:MenuItem)<-[:SERVES]-(r:Restaurant)
                   -[:HAS_CUISINE]->(c:Cuisine),
                   (m)-[:CONTAINS]->(i2:Ingredient)
             WHERE i1.id < i2.id
             WITH i1, i2, collect(DISTINCT c.name) AS cuisines
             MATCH (i1)-[rel:PAIRS_WITH]-(i2)
             RETURN rel, cuisines",
            "SET rel.cuisines = cuisines",
            {batchSize: $batch, iterateList: false}
        )
        YIELD batches, total, timeTaken, committedOperations
        RETURN batches, total, timeTaken, committedOperations
        """,
        batch=APOC_BATCH,
    )
    row = result.single()
    print(f"  Tagged {row['committedOperations']:,} edges "
          f"in {row['batches']} batches ({row['timeTaken']:.1f}s server-side)")


def create_pairs_index(session):
    session.run(
        """
        CREATE INDEX pairs_frequency IF NOT EXISTS
        FOR ()-[r:PAIRS_WITH]-() ON (r.frequency)
        """
    )
    print("  Created index on PAIRS_WITH.frequency")


def print_summary(session):
    row = session.run(
        """
        MATCH ()-[r:PAIRS_WITH]-()
        RETURN count(r)/2 AS total_pairs,
               max(r.frequency) AS max_freq,
               avg(r.frequency) AS avg_freq
        """
    ).single()
    print(f"\nPAIRS_WITH summary:")
    print(f"  Total pairs : {row['total_pairs']:,}")
    print(f"  Max freq    : {row['max_freq']}")
    print(f"  Avg freq    : {row['avg_freq']:.1f}")


def main():
    print("Connecting to Neo4j...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    try:
        driver.verify_connectivity()
        print("Connected.\n")
    except Exception as e:
        print(f"Cannot connect: {e}")
        sys.exit(1)

    with driver.session() as session:
        compute_pairs(session)
        tag_cuisine_pairs(session)
        create_pairs_index(session)
        print_summary(session)

    driver.close()
    print("\nDone. Graph is ready for agent queries.")


if __name__ == "__main__":
    main()
