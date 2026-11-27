# Shopee fixtures (Apify, shopee.vn, captured 2026-11-27)

Real outputs of the two Apify actors used by the Shopee plugin. Reviewer names were replaced
with `user_N` / `anon_N`; nothing else was changed. Do not add files containing tokens.

| File | Actor / call | Notes |
|---|---|---|
| `apify_cliqtomedia_search_vn.json` | `cliqtomedia~shopee-scraper`, `mode:"search"`, 2 keywords, `sortBy:"sales"` | Cards: `rating` present, `ratingCount`/`sold` null |
| `apify_cliqtomedia_product_detail_vn.json` | same, `mode:"product"`, `includeDetails:true`, 4 products | `rating` null for 3 of 4, `sold` always null |
| `apify_cliqtomedia_shop_vn.json` | same, `mode:"shop"`, shop 1937969974 | 1 `recordType:"shop"` row + product cards with `rating` |
| `apify_themineworks_reviews_4products_vn.json` | `themineworks~shopee-reviews-scraper`, 4 products, max 150, no filters | Includes `_type` rows: `product_summary` (coverage `complete` / `stopped_at_max` / `shopee_limit`), `summary`, `info` |
| `apify_themineworks_reviews_comment_only_vn.json` | same, `withCommentOnly:true`, max 20 | Summary row shows `reviews_read` 73, `skipped_by_comment_filter` 27, billed 20 |
| `apify_themineworks_reviews_stars123_6months_vn.json` | same, `stars:["1","2","3"]`, `withCommentOnly`, `newerThan:"6 months"`, max 10 | Billed 10 of 182 read |
| `apify_run_succeeded.json` | `GET /v2/actor-runs/{id}` (trimmed) | `usageTotalUsd`, `chargedEventCounts` |
| `apify_error_invalid_input.json` | `POST /v2/acts/.../runs` with integer `stars` | `invalid-input` error shape |

Products: `1801204805/49867965494` (small shop, 0 reviews), `1937969974/48571216193` (mid shop, complete),
`158660528/27551742889` (Mall, stopped at max), `18644537/17979591922` (very large shop, `shopee_limit`).
