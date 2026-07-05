// ============================================================
// CONSTRAINTS — enforce uniqueness and speed up MERGE lookups
// ============================================================

CREATE CONSTRAINT restaurant_id IF NOT EXISTS
FOR (r:Restaurant) REQUIRE r.id IS UNIQUE;

CREATE CONSTRAINT menu_item_id IF NOT EXISTS
FOR (m:MenuItem) REQUIRE m.id IS UNIQUE;

CREATE CONSTRAINT ingredient_id IF NOT EXISTS
FOR (i:Ingredient) REQUIRE i.id IS UNIQUE;

CREATE CONSTRAINT gmi_id IF NOT EXISTS
FOR (g:GMI) REQUIRE g.id IS UNIQUE;

CREATE CONSTRAINT cuisine_name IF NOT EXISTS
FOR (c:Cuisine) REQUIRE c.name IS UNIQUE;

CREATE CONSTRAINT neighborhood_name IF NOT EXISTS
FOR (n:Neighborhood) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT ingredient_category_name IF NOT EXISTS
FOR (ic:IngredientCategory) REQUIRE ic.name IS UNIQUE;

CREATE CONSTRAINT price_tier_name IF NOT EXISTS
FOR (pt:PriceTier) REQUIRE pt.name IS UNIQUE;

CREATE CONSTRAINT chain_name IF NOT EXISTS
FOR (ch:Chain) REQUIRE ch.name IS UNIQUE;

CREATE CONSTRAINT flavor_profile_name IF NOT EXISTS
FOR (fp:FlavorProfile) REQUIRE fp.name IS UNIQUE;

CREATE INDEX ingredient_flavor IF NOT EXISTS
FOR (i:Ingredient) ON (i.name);

// ============================================================
// INDEXES — speed up frequent query patterns
// ============================================================

CREATE INDEX restaurant_type IF NOT EXISTS
FOR (r:Restaurant) ON (r.type);

CREATE INDEX restaurant_city IF NOT EXISTS
FOR (r:Restaurant) ON (r.city);

CREATE INDEX menu_item_price IF NOT EXISTS
FOR (m:MenuItem) ON (m.price);

CREATE INDEX menu_item_name IF NOT EXISTS
FOR (m:MenuItem) ON (m.name);

CREATE INDEX ingredient_name IF NOT EXISTS
FOR (i:Ingredient) ON (i.name);

CREATE INDEX ingredient_category IF NOT EXISTS
FOR (i:Ingredient) ON (i.category);

CREATE INDEX gmi_category IF NOT EXISTS
FOR (g:GMI) ON (g.category);
