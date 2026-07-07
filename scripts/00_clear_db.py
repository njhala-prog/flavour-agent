"""
Wipe all nodes and relationships from Neo4j before a fresh reload.

Run BEFORE 02_load_data.py when switching to new CSV data:
    python scripts/00_clear_db.py
"""

import os
import sys
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

BATCH = 10_000


def clear(driver):
    with driver.session() as session:
        while True:
            result = session.run(
                f"MATCH (n) WITH n LIMIT {BATCH} DETACH DELETE n RETURN count(n) AS deleted"
            )
            deleted = result.single()["deleted"]
            print(f"  Deleted {deleted:,} nodes...")
            if deleted == 0:
                break
    print("Database cleared.")


def main():
    print("Connecting to Neo4j...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        driver.verify_connectivity()
        print("Connected.\n")
    except Exception as e:
        print(f"Cannot connect: {e}")
        sys.exit(1)

    confirm = input("This will DELETE ALL data. Type 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        sys.exit(0)

    clear(driver)
    driver.close()


if __name__ == "__main__":
    main()
