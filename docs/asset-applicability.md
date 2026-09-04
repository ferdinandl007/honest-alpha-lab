# Asset applicability and source-to-security mapping

An alpha generator must answer two different questions:

1. Is an observation economically informative?
2. Which securities could it have informed at that point in history?

The first is a hypothesis. The second is an independently governed, point-in-time data problem. Honest Alpha Lab never turns an external observation directly into a stock signal until both are explicitly recorded.

## Flow

```text
Discovery agent
  -> lawful source candidate + causal hypothesis + source key
  -> independent data steward approves source
  -> feature builder preserves first publication time
  -> mapping agent proposes source-key -> asset exposure rule
  -> independent data steward approves dated exposure map
  -> point-in-time mapper intersects source availability, map availability,
     historical universe membership, and decision time
  -> security-level feature -> numerical validation -> Alpha Registry
```

Agents may propose source keys and mappings. They cannot approve a data source, exposure map, alpha, evaluation, sealed test, or portfolio action.

## What a map contains

Each row records an asset, a source key, the exposure type and signed weight, its valid period, the timestamp at which that relationship was knowable, a reviewable evidence URI, and the mapping-data lineage. A mapping is eligible only after an independent `data-steward` approves it against the approved source dataset.

The mapper requires all of these conditions at the decision timestamp:

- the external feature was observed and first published;
- the mapping row was already available and historically valid;
- the security was in the historical research universe; and
- the source dataset and map are both independently approved.

This prevents, for example, using a later segment disclosure to infer that a retailer had a past category exposure, or using today’s constituents to create an old industry basket.

## Examples

| Signal domain | Source key | Mapping evidence | Typical exposure |
| --- | --- | --- | --- |
| Accounting / filings | CIK or historical issuer-security identifier | SEC filing, historical identifier/security master | Direct issuer (normally 1.0) |
| Sector or industry series | PIT GICS/NAICS/SIC group | Historical classification master | Sector/industry basket; verify it is more informative than the sector control |
| Retail activity | product category, store/facility or geography | dated segment revenue disclosures, store roster, corporate filings | category, geography, or facility share |
| Air traffic / disruption | airport, route, hub | dated carrier route and hub exposure disclosures | route-or-hub share |
| Energy operations | refinery/facility or PADD | ownership records and operating disclosures | facility/operator exposure |
| Supply chain | supplier/customer/product | dated filings, contractual disclosures, licensed relationship source | supplier/customer weight |

The 15 current alternative sleeves already name their required maps (`issuer_basket_map`, `airline_exposure_map`, `retailer_exposure_map`, and `airline_hub_exposure_map`). They remain proposals until those maps and source vintages exist.

## Google Maps / Places is not a shortcut

Google Maps Platform data is not automatically a research dataset. A connector must remain a typed, credential-isolated provider adapter; it may not scrape Google products or silently retain raw provider content. Before any trial, the data steward must confirm the exact contract, permitted storage/derived-feature use, point-in-time reproducibility, cost, attribution, and whether the source includes personal data. Google’s Places policies place storage/caching and display/attribution constraints on content, and the current platform terms contain additional usage restrictions. See Google’s [Places API policies](https://developers.google.com/maps/documentation/places/web-service/policies?hl=en) and [Maps Platform terms](https://cloud.google.com/maps-platform/terms).

For an approved aggregate location series, the mapping still has to be explicit: for example `place_id -> facility -> historical parent issuer -> listed security`, with a dated ownership/operating weight. A score based only on “near stores belonging to a retailer” is not sufficient without an archived, licensed, and time-valid relationship table.

## Validation requirements

Alternative-data validation includes an `asset-applicability-map` check in addition to normal PIT, purged walk-forward, regime, multiple-testing, and sealed-test checks. It must report map hash, evidence, exposure availability, mapping concentration, and ablations such as direct issuer vs industry-only vs geographic/facility/route mapping. The candidate fails if its incremental information is only a disguised sector effect or depends on a map created with future knowledge.
