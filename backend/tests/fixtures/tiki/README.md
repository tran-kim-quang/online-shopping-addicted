# Tiki API Fixtures

Sanitized JSON fixtures captured from live Tiki public web APIs on **2026-11-25** (rate ≤ 1 req/s).

## Files

| File | Endpoint | Notes |
|---|---|---|
| `product_detail.json` | `GET https://tiki.vn/api/v2/products/277880353?platform=web&spid=277880354` | Trimmed to relevant product/seller/spec/breadcrumb/image fields |
| `reviews_page1.json` | `GET https://tiki.vn/api/v2/reviews?product_id=277880353&page=1&limit=20&...` | 5 reviews, paging adjusted for 2-page pagination tests |
| `reviews_page2.json` | `GET https://tiki.vn/api/v2/reviews?product_id=277880353&page=2&limit=20&...` | 3 reviews, `current_page=2, last_page=2` |
| `shop_info.json` | `GET https://tiki.vn/api/v2/seller/stores/tiki-trading` | Slug → `seller_id` + `store_id` mapping |
| `shop_widget.json` | `GET https://tiki.vn/api/shopping/v2/widgets/seller?seller_id=1` | Shop rating, follower count, official flag, canonical URL |
| `shop_listing_page1.json` | `GET https://tiki.vn/api/v2/products?limit=10&page=1&seller=1&sort=top_seller` | 5 top-selling products from Tiki Trading |
| `product_not_found.json` | `GET https://tiki.vn/api/v2/products/999999999999?platform=web` | Real HTTP 404 JSON error response |
| `shop_not_found.json` | `GET https://tiki.vn/api/v2/seller/stores/this-shop-does-not-exist-99999` | Real HTTP 200 response with `seller_id: null` |

## Sanitization

- Removed `customer_id`, `created_by.id`, `created_by.avatar_url`, `created_by.region`, `created_by.contribute_info`, `comments`, and `timeline` from all reviews.
- Shortened reviewer display names to surname + initials (e.g. `"Nguyễn T. T."`).
- Trimmed large non-essential tracking/ad/badge payloads to keep fixtures compact.
