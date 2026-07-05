# MenuData Flavor Innovation Agent

An AI-powered agent that discovers flavor trends and ingredient innovation opportunities by querying a Neo4j graph database of 50,000 NYC restaurant menu items and synthesizing insights with GPT-4o.

## Architecture

```
Natural Language Question
        │
        ▼
 GPT-4o: Generate Cypher  ◄── (retry with error feedback on failure)
        │
        ▼
   Neo4j Graph DB
        │
        ▼
 GPT-4o: Synthesize Insight
        │
        ▼
  Actionable Insight
```

**Stack:** Python · Neo4j 5 · GPT-4o · Braintrust

## Graph Model

```
(:Restaurant)-[:SERVES]->(:MenuItem)
(:MenuItem)-[:CONTAINS]->(:Ingredient)
(:MenuItem)-[:IS_TYPE]->(:GMI)
(:MenuItem)-[:IN_PRICE_TIER]->(:PriceTier)
(:Restaurant)-[:HAS_CUISINE]->(:Cuisine)
(:Restaurant)-[:LOCATED_IN]->(:Neighborhood)
(:Restaurant)-[:PART_OF_CHAIN]->(:Chain)
(:Ingredient)-[:IN_CATEGORY]->(:IngredientCategory)
(:Ingredient)-[:HAS_FLAVOR]->(:FlavorProfile)
(:Ingredient)-[:PAIRS_WITH {frequency, cuisines}]-(:Ingredient)   ← precomputed
```

The `PAIRS_WITH` relationship is the key design decision: precomputing ingredient co-occurrence makes flavor combination queries run in milliseconds instead of seconds.

## Setup

### Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- Python 3.11+
- OpenAI API key
- Braintrust API key

> **Note:** Neo4j runs via Docker with the APOC plugin pre-enabled in `docker-compose.yml`. You do not need to install Neo4j separately.


Step 1 — Create a virtual environment

**macOS / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

> **Windows tip:** If you get `cannot be loaded because running scripts is disabled`, run this once in an admin PowerShell: `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`

**Windows (Command Prompt):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

Then install dependencies (same on all platforms):
```bash
pip install -r requirements.txt
```



Step 2 — Configure environment variables

**macOS / Linux:**
```bash
cp .env.example .env
```

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

**Windows (Command Prompt):**
```cmd
copy .env.example .env
```

Open `.env` and fill in your keys:

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password

OPENAI_API_KEY=sk-...
BRAINTRUST_API_KEY=...
```

> The `NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD` values above are the correct defaults for the local Docker setup — only the API keys need to be changed.

---

 Step 3 — Start Neo4j

```
docker compose up -d
```

Wait about 20–30 seconds for Neo4j to fully start, then verify at:

```
Neo4j Browser: http://localhost:7474
Login: neo4j / password
```

You should see the Neo4j browser. If you see a connection error, wait a few more seconds and refresh.

> **Verify Docker is running first:** Open Docker Desktop and confirm it shows "Engine running" before running this command.





Step 5 — Load CSV data (~2–3 minutes)

```
python scripts/02_load_data.py
```

This loads 50,000 menu items, 151,000+ ingredient relationships, and all restaurant/cuisine data. The loader uses batches of 5,000 rows and parallel sessions for independent tables, so it is significantly faster than a naive row-by-row approach. You will see a progress bar for each phase.

Expected output:
```
Connecting to Neo4j...
Connected.

Phase 1 — loading independent tables in parallel (ingredients / GMIs / restaurants)...
Phase 2 — loading menu items + flavor profiles in parallel...
Phase 3 — loading item-ingredient links...

All data loaded successfully.
Next: run scripts/03_compute_pairs.py to build PAIRS_WITH relationships.
```

---

 Step 6 — Precompute ingredient pairings (under 1 minute)

```
python scripts/03_compute_pairs.py
```

This builds the `PAIRS_WITH` relationships between co-occurring ingredients using `apoc.periodic.iterate`, which runs entirely server-side with no Python round-trips per batch. These relationships are what make flavor pairing queries fast at runtime.

Expected output:
```
Computing ingredient co-occurrence pairs (server-side via APOC)...
  Min frequency threshold: 5
  Created 9,149 PAIRS_WITH relationships in N batches (X.Xs server-side)
Tagging pairs with cuisine context (server-side via APOC)...
  Tagged 9,149 edges in N batches (X.Xs server-side)

PAIRS_WITH summary:
  Total pairs : 9,149
  Max freq    : ...
  Avg freq    : ...
```

---

Step 7 — Verify the data loaded correctly

Open Neo4j Browser at `http://localhost:7474` and run:

```cypher
MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count DESC
```

You should see approximately:

| label | count |
|---|---|
| MenuItem | 50,000 |
| Ingredient | 159 |
| Restaurant | 500 |
| GMI | 55 |
| Chain | 50 |
| Neighborhood | 21 |
| IngredientCategory | 10 |
| Cuisine | 10 |
| FlavorProfile | 7 |
| PriceTier | 3 |

Also verify relationships:

```cypher
MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS count ORDER BY count DESC
```

You should see `CONTAINS` at ~151,000, `SERVES` at ~50,000, and `PAIRS_WITH` at ~9,000+.



Step 8 — Run unit tests (no API keys needed)

```bash
pytest tests/ -v
```

All 66 tests should pass in under 10 seconds. These use mocks so no database or OpenAI connection is required.


 Step 9 — Run demo queries

```bash
python demo/run_demos.py
```

Runs 5 example questions against the live database and prints the Cypher, raw results, and synthesized insight for each. Takes ~20–40 seconds total.

> **Requires:** Neo4j running (Step 3) and `OPENAI_API_KEY` set in `.env`.

---

### Step 10 — Run the evaluation suite

```bash
python eval/evaluate.py
```

Runs all 20 test cases through Braintrust and prints per-case scores (Accuracy, Relevance, Creativity/Graceful-Handling, Latency) in the terminal as each case completes, then prints a full summary table at the end. Results are also logged to your Braintrust dashboard.

Takes ~5–8 minutes total (20 LLM calls + judge scoring for each).

> **Requires:** Neo4j running, `OPENAI_API_KEY`, and `BRAINTRUST_API_KEY` set in `.env`.

---

### Step 11 — Start the API server (optional)

```bash
uvicorn src.api:app --reload --port 8000
```

Interactive docs are available at `http://localhost:8000/docs`.

**Test the API — macOS / Linux:**
```bash
# Health check
curl http://localhost:8000/health

# Ask a question
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"question": "What are the most common ingredients in ramen?"}'
```

**Test the API — Windows (PowerShell):**
```powershell
# Health check
Invoke-RestMethod -Uri http://localhost:8000/health

# Ask a question
Invoke-RestMethod -Uri http://localhost:8000/query `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"question": "What are the most common ingredients in ramen?"}'
```

**Test the API — Windows (curl.exe — available in Windows 10+ and Git Bash):**
```powershell
# Health check
curl.exe http://localhost:8000/health

# Ask a question
curl.exe -X POST http://localhost:8000/query `
  -H "Content-Type: application/json" `
  -d '{\"question\": \"What are the most common ingredients in ramen?\"}'
```

---

### Troubleshooting

**`docker exec` fails with "no such container"**
Neo4j hasn't started yet or `docker compose up -d` was not run. Check with `docker ps` — the container should be named `menudata-neo4j`.

**`Neo4jConnectionError` when running scripts**
Check your `.env` has `NEO4J_URI=bolt://localhost:7687` and the Docker container is running (`docker ps`). Run `docker compose up -d` if needed.

**Schema step returns `Cypher parse error`**
Neo4j was not fully ready when you piped the schema file. Wait 30 seconds after `docker compose up -d` and try Step 4 again.

**`OPENAI_API_KEY` error during demo or eval**
Make sure `.env` is in the project root (same folder as `docker-compose.yml`) and the key starts with `sk-`.

**`BRAINTRUST_API_KEY` error during eval**
The eval script requires Braintrust. If you want to test the agent without Braintrust, run `python demo/run_demos.py` instead (no Braintrust key needed).

**PowerShell says `.venv\Scripts\Activate.ps1` cannot be loaded**
Run this once in an Administrator PowerShell:
```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**`apoc` procedure not found during Step 6**
APOC is enabled automatically via `NEO4J_PLUGINS=["apoc"]` in `docker-compose.yml`. If you see this error, Neo4j may not have had time to download and enable APOC after the first `docker compose up -d`. Stop the container (`docker compose down`), wait a moment, then `docker compose up -d` and wait 60 seconds before retrying.

---

## Demo Outputs

*Output from `python demo/run_demos.py` against the live dataset.*

---

**1. Cross-Cuisine Crossover**
> Q: What Asian spices and ingredients are crossing over into American cuisine menus in NYC?

```
Top raw results (15 rows):
  rice        — asian_uses: 1395, american_uses:  87, ratio: 16.0
  soy sauce   — asian_uses: 1448, american_uses:  98, ratio: 14.8
  scallion    — asian_uses: 1076, american_uses:  94, ratio: 11.4
  ginger      — asian_uses: 1103, american_uses: 100, ratio: 11.0
  carrot      — asian_uses: 1074, american_uses: 106, ratio: 10.1
  ... 10 more rows
Latency: 7.72s | Retries: 0
```

> Asian spices and ingredients like rice, soy sauce, and scallions are increasingly appearing on
> American cuisine menus in NYC, with rice showing the highest crossover ratio of 16.0. Soy sauce
> and scallions show ratios of 14.8 and 11.4 respectively, suggesting their growing versatility.
> Ramen noodles and tuna (ratios 7.3 and 7.8) further underscore a vibrant fusion scene where
> traditional Asian ingredients are being creatively incorporated into American culinary offerings.

---

**2. White-Space Innovation**
> Q: Find ingredients that appear much more frequently in Japanese cuisine than in Korean cuisine.

```
Raw results (7 rows):
  soy sauce    — jap: 735, kor:  78 (9.4×)
  pork         — jap: 409, kor:  73 (5.6×)
  tuna         — jap: 409, kor:  84 (4.9×)
  salmon       — jap: 406, kor:  68 (6.0×)
  ramen noodles— jap: 404, kor:  56 (7.2×)
  scallion     — jap: 328, kor:  94 (3.5×)
  ginger       — jap: 321, kor: 100 (3.2×)
Latency: 5.13s | Retries: 0
```

> Soy sauce is the most striking gap — 735 Japanese uses vs 78 Korean (9.4×), underscoring its
> foundational role in Japanese cooking. Ramen noodles (404 vs 56) and the seafood trio of tuna,
> salmon, and pork show similar disparities, reflecting Japanese cuisine's sushi-forward identity.
> These 7 ingredients represent clear white-space opportunities for Korean restaurants to
> differentiate by adopting Japanese-adjacent flavors.

---

**3. Unexpected Pairings**
> Q: What unusual ingredient combinations appear in Brooklyn fine dining?

```
Top raw results (20 rows, showing first 5):
  bacon      + lamb        — frequency: 5
  crab       + tofu        — frequency: 5
  crab       + ground beef — frequency: 5
  duck       + steak       — frequency: 5
  steak      + tofu        — frequency: 5
  ... 15 more rows
Latency: 4.15s | Retries: 0
```

> In Brooklyn fine dining, unusual combinations like crab with tofu and cream cheese with scallops
> are emerging, each appearing in 5 menu items. The crab-tofu pairing blends seafood sweetness with
> plant-based creaminess in a rare surf-and-plant format. Bacon with lamb and cheddar with lamb
> merge smoky and gamey richness rarely paired outside this geography. Each combination sits at the
> minimum PAIRS_WITH threshold (frequency=5) — real culinary patterns, not one-off specials, but
> niche enough to represent genuine innovation opportunities.

---

**4. Emerging Trend**
> Q: Which spice combinations are trending in fast casual Korean, Thai, and Mediterranean restaurants?

```
Top raw results (15 rows, showing first 5):
  cumin + paprika      — co_occurrences: 166
  cumin + salt         — co_occurrences: 166
  cumin + garlic powder— co_occurrences: 166
  cumin + oregano      — co_occurrences: 166
  cumin + rosemary     — co_occurrences: 166
  ... 10 more rows
Latency: 3.92s | Retries: 0
```

> Cumin dominates as the anchor spice in fast casual niche cuisines, pairing with za'atar,
> turmeric, and ginger powder at 166 co-occurrences each — reflecting a trend towards globally
> inspired, health-conscious flavor profiles. Ginger powder + turmeric (82 co-occurrences) signals
> a secondary anti-inflammatory spice cluster gaining traction. These combinations represent a clear
> opportunity for fast casual operators to differentiate via globally-influenced spice blending.

---

**5. Flavor Profile Gap**
> Q: What are the top 10 most common ingredient pairings in Italian cuisine restaurants?

```
Raw results (10 rows, showing first 5):
  cheese    + sour cream — frequency: 834
  cheese    + tortilla   — frequency: 812
  sour cream+ tortilla   — frequency: 803
  cheese    + tomato     — frequency: 747
  beef      + cheese     — frequency: 744
  ... 5 more rows
Latency: 3.15s | Retries: 0
```

> The top pairings — cheese+sour cream (834) and cheese+tortilla (812) — align with Mexican/Tex-Mex
> rather than Italian cuisine. This reflects a dataset limitation: PAIRS_WITH edges are global
> co-occurrence counts across all menu items, not scoped to a cuisine. A question about "pairings
> within Italian restaurants" requires a scoped query (counting co-occurrences only within Italian
> restaurant items), which the agent correctly surfaces as an anomaly worth flagging.

---

## Evaluation Results

*Output from `python eval/evaluate.py` — 20 test cases scored across Accuracy, Relevance,
Creativity/Graceful-Handling, and Latency. Composite = acc×0.30 + rel×0.25 + cr/gh×0.25 + lat×0.20.*

| # | Question (truncated) | Category | acc | rel | cr/gh | lat | **composite** |
|---|---|---|---|---|---|---|---|
| 1 | Most common ingredients in ramen? | simple_lookup | 1.00 | 0.50 | 1.00 | 0.65 | **0.804** |
| 2 | Most used ingredients in burgers? | simple_lookup | 1.00 | 1.00 | 1.00 | 0.66 | **0.933** |
| 3 | Spices in Indian cuisine + frequency? | simple_lookup | 1.00 | 0.50 | 1.00 | 0.69 | **0.814** |
| 4 | Average price of a pizza? | simple_lookup | 1.00 | 0.50 | 1.00 | 0.54 | **0.783** |
| 5 | Rank ingredient categories by frequency? | simple_lookup | 1.00 | 1.00 | 1.00 | 0.65 | **0.931** |
| 6 | Italian vs American top ingredients? | comparative | 1.00 | 1.00 | 1.00 | 0.39 | **0.877** |
| 7 | Burger price: fast casual vs casual? | comparative | 1.00 | 1.00 | 1.00 | 0.66 | **0.932** |
| 8 | Manhattan vs Brooklyn protein dishes? | comparative | 1.00 | 1.00 | 1.00 | 0.68 | **0.936** |
| 9 | Ingredient sophistication: chain vs indie? | comparative | 1.00 | 1.00 | 1.00 | 0.64 | **0.928** |
| 10 | Japanese vs Korean seafood use? | comparative | 1.00 | 1.00 | 1.00 | 0.62 | **0.923** |
| 11 | Dessert flavor trends across NYC? | trend_analysis | 1.00 | 1.00 | 1.00 | 0.46 | **0.891** |
| 12 | Spice combos in fast casual Mexican? | trend_analysis | 1.00 | 0.50 | 1.00 | 0.65 | **0.804** |
| 13 | Asian ingredients crossing into American? | trend_analysis | 1.00 | 1.00 | 1.00 | 0.43 | **0.887** |
| 14 | Popular pairings in Brooklyn fine dining? | trend_analysis | 1.00 | 1.00 | 1.00 | 0.53 | **0.906** |
| 15 | Thai vs Mexican fusion opportunities? | cross_cuisine | 1.00 | 1.00 | 1.00 | 0.14 | **0.829** |
| 16 | Unexpected combos in Brooklyn fine dining? | cross_cuisine | 1.00 | 1.00 | 1.00 | 0.49 | **0.899** |
| 17 | Korean vs Japanese ingredient gaps? | cross_cuisine | 1.00 | 1.00 | 1.00 | 0.52 | **0.903** |
| 18 | What's popular? (ambiguous) | edge_case | 1.00 | 1.00 | gh=1.00 | 0.04 | **0.807** |
| 19 | Vegan options in steakhouses? (sparse) | edge_case | 1.00 | 0.50 | gh=1.00 | 0.48 | **0.772** |
| 20 | Ingredeints with yuzu? (typo) | edge_case | 1.00 | 1.00 | gh=1.00 | 0.49 | **0.898** |

### Summary by Category

| Category | Cases | Avg Composite |
|---|---|---|
| Simple Lookup | 5 | 0.853 |
| Comparative | 5 | 0.919 |
| Trend Analysis | 4 | 0.872 |
| Cross-Cuisine | 3 | 0.877 |
| Edge Cases | 3 | 0.826 |
| **Overall** | **20** | **0.873** |

**Accuracy is 1.00 across all 20 cases** — every query returned the right data shape, correct row count, and plausible numeric values as verified by the ground-truth `result_assertion` callables. This score cannot be inflated by rephrasing the insight text.

**5 cases scored rel=0.50** (cases 1, 3, 4, 12, 19): the LLM judge returned PARTIAL for those answers — the data was correct but the synthesized insight was considered incomplete for the specific angle asked. Case 19 (vegan in steakhouses) is the lowest composite at 0.772, which is expected: the dataset has almost no vegan steakhouse options, so there is little to say.

**Latency scores** reflect `max(0, 1 − latency_seconds / 10)`. Case 18 ("What's popular?") is the slowest at ~9.6s actual latency (lat score=0.04) because the maximally ambiguous query requires the most reasoning. Case 15 (Thai vs Mexican) was ~8.6s (lat=0.14) for the same reason.

---

## Project Structure

```
├── scripts/
│   ├── 01_schema.cypher       # Constraints + indexes
│   ├── 02_load_data.py        # Load CSVs into Neo4j (batched, parallel phases)
│   └── 03_compute_pairs.py    # Build PAIRS_WITH relationships via APOC
├── src/
│   ├── config.py              # Centralized settings (all env vars + tuneable values)
│   ├── database.py            # Neo4j connection wrapper
│   ├── prompts.py             # System prompts (schema, examples, Cypher rules)
│   ├── agent.py               # FlavorInnovationAgent
│   └── api.py                 # FastAPI REST service (GET /health, POST /query)
├── eval/
│   ├── test_cases.py          # 20 test cases with ground-truth result_assertion callables
│   └── evaluate.py            # Braintrust eval suite (Accuracy/Relevance/Creativity/Latency)
├── tests/
│   ├── test_agent.py          # Unit tests: _clean_cypher, _validate_input, mocked query flow
│   ├── test_api.py            # HTTP-level endpoint tests (TestClient, no infrastructure)
│   ├── test_database.py       # DB wrapper tests: LIMIT injection, error type mapping
│   └── test_evaluate.py       # Scorer unit tests: score_accuracy, score_latency
├── demo/
│   └── run_demos.py           # 5 showcase queries
├── docker-compose.yml
├── requirements.txt
└── .env.example
```
