"""
dealership_agent.py — CLI agent that scrapes dealership inventories from
Dealer.com and Team Velocity sites and generates a comparison report using
Claude.

Usage:
    export ANTHROPIC_API_KEY=...
    python dealership_agent.py smythevolvocars.com volvocarsprinceton.com
    python dealership_agent.py smythevolvocars.com --condition used --cache inv.db

Port of the n8n `dealership_inventory_scraper_with_agent` workflow:
    homepage fetch -> platform detection (regex) -> per-platform inventory
    fetch -> normalize -> [optional SQLite cache] -> aggregate stats ->
    build prompt -> Claude -> markdown report on stdout.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from statistics import median
from typing import Optional

from curl_cffi import requests as cffi
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# We rely on curl_cffi's `impersonate="chrome131"` to set User-Agent, the full
# Chrome client-hint header set (sec-ch-ua-*), Accept-Language, Accept-Encoding,
# AND the matching TLS + HTTP/2 fingerprints. That last bit is what gets us
# past Cloudflare-style bot protection on dealer sites.
IMPERSONATE = "chrome131"

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000
CONCURRENCY = 4
HTTP_TIMEOUT = 30.0

# Dealer.com pagination. The widget API accepts pageSize up to ~100 in our
# testing. If you find a site that paginates by smaller increments (e.g. 8 to
# match the UI grid), drop DEALER_COM_PAGE_SIZE to that number.
# DEALER_COM_MAX_PAGES is a safety cap — at 60 pages * 100 = 6000 vehicles.
DEALER_COM_PAGE_SIZE = 100
DEALER_COM_MAX_PAGES = 60

log = logging.getLogger("dealership_agent")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Vehicle:
    vin: str
    make: str
    model: str
    trim: str
    year: Optional[int]
    condition: str
    price: str
    odometer: str
    exterior_color: str
    dealership_domain: str


@dataclass
class DealershipScrape:
    domain: str
    table_name: str
    site_id: Optional[str] = None
    platform: Optional[str] = None  # "dealer_com" | "team_velocity"
    vehicles: list[Vehicle] = field(default_factory=list)
    tv_meta: Optional[dict] = None       # Team Velocity rich filter facets
    dc_total_count: Optional[int] = None  # Dealer.com pageInfo.totalCount
    error: Optional[str] = None


def domain_to_table_name(domain: str) -> str:
    """smythevolvocars.com -> cars_smythevolvocars_com"""
    safe = re.sub(r"[^a-zA-Z0-9_]", "", re.sub(r"[.\-]", "_", domain))
    return f"cars_{safe.lower()}"


# ---------------------------------------------------------------------------
# Platform detection (regex on homepage HTML)
# ---------------------------------------------------------------------------

_TV_API_RE = re.compile(
    r"inventoryApiBaseUrl\s*=\s*[\"']https://websites\.api\.teamvelocityportal\.com",
    re.I,
)
_TV_ACCOUNT_RE = re.compile(r"var\s+accountId\s*=\s*[\"'](\d+)[\"']", re.I)

# Ordered list of (label, pattern) for Dealer.com siteId extraction
_DEALER_COM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("siteId-kv", re.compile(r"""[\"']siteId[\"']\s*[,:=]\s*[\"']([^\"']+)[\"']""", re.I)),
    ("data-site-id", re.compile(r"""data-site-id=[\"']([^\"']+)[\"']""", re.I)),
    (
        "dealerId-kv",
        re.compile(
            r"""[\"']?(?:dealerId|dealer_id|site_id|siteID)[\"']?\s*[,:=]\s*[\"']([a-zA-Z][a-zA-Z0-9_-]*)[\"']""",
            re.I,
        ),
    ),
    (
        "inventoryConfig",
        re.compile(
            r"""inventoryConfig[^}]*siteId[\"']?\s*[,:=]\s*[\"']([^\"']+)[\"']""", re.I
        ),
    ),
    (
        "ddc-defined-page",
        re.compile(r"""DDC\.defined\.page\.siteId\s*=\s*[\"']([^\"']+)[\"']""", re.I),
    ),
    (
        "meta-tag",
        re.compile(
            r"""<meta[^>]*name=[\"'](?:site-id|siteId|dealer-id)[\"'][^>]*content=[\"']([^\"']+)[\"']""",
            re.I,
        ),
    ),
    (
        "franchiseId",
        re.compile(
            r"""[\"']?franchiseId[\"']?\s*[,:=]\s*[\"']([a-zA-Z][a-zA-Z0-9_-]*)[\"']""",
            re.I,
        ),
    ),
    ("api-call", re.compile(r"""/api/[^\"']*siteId=([^&\"']+)""", re.I)),
    (
        "window-config",
        re.compile(
            r"""window\.[A-Z_]+\s*=\s*\{[^}]*[\"']siteId[\"']\s*:\s*[\"']([^\"']+)[\"']""",
            re.I,
        ),
    ),
]


def detect_platform(html: str, domain: str) -> tuple[str, str]:
    """Return (platform, site_id). Raises ValueError if neither detects."""
    # Strong Team Velocity signal: API base URL + numeric accountId
    tv_api = _TV_API_RE.search(html)
    tv_account = _TV_ACCOUNT_RE.search(html)
    if tv_api and tv_account:
        return ("team_velocity", tv_account.group(1))

    # Try Dealer.com patterns in order
    for label, pat in _DEALER_COM_PATTERNS:
        m = pat.search(html)
        if not m:
            continue
        site_id = m.group(1)
        # Numeric-only IDs are almost always Team Velocity even without the
        # API base URL marker (matches n8n's post-extraction check).
        if site_id.isdigit():
            return ("team_velocity", site_id)
        log.debug("[%s] matched %s -> %s", domain, label, site_id)
        return ("dealer_com", site_id)

    # Fallback: numeric accountId alone -> Team Velocity
    if tv_account:
        return ("team_velocity", tv_account.group(1))

    raise ValueError(
        f"Could not extract siteId/accountId from {domain}. "
        "The site may not be a supported platform."
    )


# ---------------------------------------------------------------------------
# Per-platform inventory fetchers
# ---------------------------------------------------------------------------


# curl_cffi's `impersonate=` already sets User-Agent, Accept-Language,
# Accept-Encoding, and the sec-ch-ua-* client hints to match real Chrome.
# We only set request-shape-specific headers (navigation vs XHR) below.


def _client():
    return cffi.Session(
        impersonate=IMPERSONATE,
        timeout=HTTP_TIMEOUT,
        allow_redirects=True,
    )


# Headers a real browser sends on a top-level navigation (typing URL / new tab)
_NAV_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Cache-Control": "max-age=0",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Priority": "u=0, i",
}

# Headers a real browser sends on fetch/XHR calls from a same-origin page
_XHR_HEADERS = {
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


def fetch_homepage(domain: str) -> str:
    with _client() as c:
        r = c.get(f"https://www.{domain}", headers=_NAV_HEADERS)
        r.raise_for_status()
        return r.text


# Reverse-engineered from the n8n flow. The widget API is picky about this
# attribute list — keep it as one big CSV.
_DEALER_COM_REQUIRED_ATTRS = (
    "accountCity,accountCountry,accountId,accountName,accountState,accountZipcode,"
    "askingPrice,attributes,autodataCaId_att_data,bed,bodyStyle,cab,carfaxIconUrl,"
    "carfaxIconUrlBlackWhite,carfaxUrl,carfaxValueBadgeAltText,categoryName,certified,"
    "chromeId_att_data,cityMpg,classification,classificationName,comments,courtesy,"
    "cpoChecklistUrl,daysOnLot,dcpaisVideoToken_att_data,deliveryDateRange,doors,"
    "driveLine,ebayAuctionId,eleadPrice,eleadPriceLessOEMCash,engine,engineSize,"
    "equipment,extColor,exteriorColor,fuelType,globalVehicleTrimId,gvLongTrimDescription,"
    "gvTrim,hasCarFaxReport,hideInternetPrice,highwayMpg,id,incentives,intColor,"
    "interiorColor,interiorColorCode,internetComments,internetPrice,inventoryDate,"
    "invoicePrice,isElectric_att_b,key,location,make,marketingTitle,mileage,model,"
    "modelCode,msrp,normalExteriorColor,normalFuelType,normalInteriorColor,numSaves,"
    "odometer,oemSerialNumber,oemSourcedMerchandisingStatus,optionCodes,options,"
    "packageCode,packages_internal,parent,parentId,paymentMonthly,payments,"
    "primary_image,propertyDescription,retailValue,saleLease,salePrice,sharedVehicle,"
    "status,stockNumber,transmission,trim,trimLevel,type,uuid,video,vin,"
    "warrantyDescription,wholesalePrice,year,cpoTier"
)


def _dealer_com_body(
    site_id: str, condition: str, start: int, page_size: int
) -> dict:
    cond_upper = condition.upper()
    cond_lower = condition.lower()
    return {
        "siteId": site_id,
        "locale": "en_US",
        "device": "DESKTOP",
        "pageAlias": f"INVENTORY_LISTING_DEFAULT_AUTO_{cond_upper}",
        "pageId": (
            f"{site_id}_SITEBUILDER_INVENTORY_SEARCH_RESULTS_AUTO_{cond_upper}_V1_1"
        ),
        "windowId": "inventory-data-bus2",
        "widgetName": "ws-inv-data",
        "inventoryParameters": {"start": [str(start)]},
        "preferences": {
            "widgetClasses": "spacing-reset",
            "pageSize": str(page_size),
            "listing.config.id": f"auto-{cond_lower}",
            "listing.boost.order": (
                "account,make,model,bodyStyle,trim,optionCodes,modelCode,fuelType"
            ),
            "removeEmptyFacets": "true",
            "removeEmptyConstraints": "true",
            "required.display.attributes": _DEALER_COM_REQUIRED_ATTRS,
            "facetInstanceId": "listing",
            "geoLocationEnabled": "false",
            "defaultGeoDistRadius": "0",
            "geoRadiusValues": "0,5,25,50,100,250,500,1000",
            "showCertifiedFranchiseVehiclesOnly": "false",
            "showFranchiseVehiclesOnly": "true",
            "sorts": "year,normalBodyStyle,normalExteriorColor,odometer,internetPrice",
            "sortsTitles": "YEAR,BODYSTYLE,COLOR,MILEAGE,PRICE",
        },
        "includePricing": True,
    }


def _fetch_dealer_com_page(
    domain: str, site_id: str, condition: str, start: int, page_size: int
) -> dict:
    """Fetch a single page of inventory. Returns the raw API response."""
    body = _dealer_com_body(site_id, condition, start, page_size)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": f"https://www.{domain}/",
        "Origin": f"https://www.{domain}",
        **_XHR_HEADERS,
    }
    with _client() as c:
        r = c.post(
            f"https://www.{domain}/api/widget/ws-inv-data/getInventory",
            json=body,
            headers=headers,
        )
        r.raise_for_status()
        return r.json()


def fetch_dealer_com(domain: str, site_id: str, condition: str) -> dict:
    """Paginate through all inventory pages and return one merged response.
    Output shape mirrors a single-page response so downstream code is unchanged.
    """
    all_vehicles: list = []
    page_info: dict = {}
    total: Optional[int] = None

    for page in range(DEALER_COM_MAX_PAGES):
        start = page * DEALER_COM_PAGE_SIZE
        try:
            resp = _fetch_dealer_com_page(
                domain, site_id, condition, start, DEALER_COM_PAGE_SIZE
            )
        except Exception as e:
            # Dealer.com sometimes returns 500 when `start` is past the actual
            # inventory size. If we already have some pages, treat it as "done"
            # and return what we have. If the first page fails, re-raise so
            # the caller knows the scrape genuinely failed.
            if all_vehicles:
                log.warning(
                    "[%s] page %d failed (%s); stopping with %d vehicles",
                    domain, page + 1, e, len(all_vehicles),
                )
                break
            raise

        page_inv = resp.get("inventory") or []
        page_info = resp.get("pageInfo") or page_info
        if total is None:
            total = page_info.get("totalCount")

        if not page_inv:
            log.debug("[%s] page %d returned 0 vehicles, stopping", domain, page + 1)
            break

        all_vehicles.extend(page_inv)
        log.info(
            "[%s] page %d: +%d vehicles (running total %d/%s)",
            domain, page + 1, len(page_inv), len(all_vehicles),
            total if total is not None else "?",
        )

        if total is not None and len(all_vehicles) >= total:
            break
        if len(page_inv) < DEALER_COM_PAGE_SIZE:
            # Short page = last page even if totalCount is missing/wrong
            break
    else:
        log.warning(
            "[%s] hit DEALER_COM_MAX_PAGES (%d); inventory may be truncated",
            domain, DEALER_COM_MAX_PAGES,
        )

    # Patch pageInfo so downstream sees the merged total in the same shape
    if page_info:
        page_info = dict(page_info)
        page_info.setdefault("pageSize", DEALER_COM_PAGE_SIZE)
    return {"inventory": all_vehicles, "pageInfo": page_info}


def fetch_team_velocity(domain: str, condition: str) -> dict:
    body = {
        "benefits": None,
        "condition": condition.lower(),
        "inTransit": False,
        "makes": None,
        "models": None,
        "selectedCondition": condition.lower(),
        "showAllFilter": False,
        "trims": None,
        "year": "",
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": f"https://www.{domain}/new-inventory/index.htm",
        "Origin": f"https://www.{domain}",
        **_XHR_HEADERS,
    }
    with _client() as c:
        r = c.post(
            f"https://www.{domain}/api/Inventory/getinventorymultiselectionfilters",
            json=body,
            headers=headers,
        )
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Normalize platform responses -> list[Vehicle] (+ platform-specific extras)
# ---------------------------------------------------------------------------


def _attr(car: dict, name: str) -> str:
    for a in car.get("trackingAttributes") or []:
        if a.get("name") == name:
            return str(a.get("value", "N/A"))
    return "N/A"


def _coerce_year(v) -> Optional[int]:
    try:
        return int(v) if v else None
    except (TypeError, ValueError):
        return None


def normalize_dealer_com(
    resp: dict, domain: str, condition: str
) -> tuple[list[Vehicle], int]:
    inventory = resp.get("inventory") or []
    page_info = resp.get("pageInfo") or {}
    total = int(page_info.get("totalCount") or len(inventory))

    vehicles: list[Vehicle] = []
    for car in inventory:
        price = (
            (car.get("trackingPricing") or {}).get("internetPrice")
            or car.get("internetPrice")
            or "N/A"
        )
        vehicles.append(
            Vehicle(
                vin=str(car.get("vin") or "N/A"),
                make=str(car.get("make") or "N/A"),
                model=str(car.get("model") or "N/A"),
                trim=str(car.get("trim") or "N/A"),
                year=_coerce_year(car.get("year")),
                condition=str(car.get("condition") or condition).lower(),
                price=str(price),
                odometer=_attr(car, "odometer"),
                exterior_color=_attr(car, "exteriorColor"),
                dealership_domain=domain,
            )
        )
    return vehicles, total


def normalize_team_velocity(
    resp: dict, domain: str, condition: str
) -> tuple[list[Vehicle], dict]:
    raw_vehicles = resp.get("vehicles") or resp.get("inventory") or []
    filters = resp.get("filters") or {}

    def _safe(key: str) -> list[dict]:
        return [x for x in (filters.get(key) or []) if x.get("value")]

    vehicles: list[Vehicle] = []
    for v in raw_vehicles:
        price = (
            v.get("price") or v.get("internetPrice") or v.get("salePrice") or "N/A"
        )
        vehicles.append(
            Vehicle(
                vin=str(v.get("vin") or v.get("VIN") or v.get("item_id") or "N/A"),
                make=str(v.get("make") or v.get("Make") or "N/A"),
                model=str(v.get("model") or v.get("Model") or "N/A"),
                trim=str(
                    v.get("trim") or v.get("Trim") or v.get("variant") or "N/A"
                ),
                year=_coerce_year(v.get("year") or v.get("Year")),
                condition=condition.lower(),
                price=str(price),
                odometer=str(v.get("mileage") or v.get("odometer") or "0"),
                exterior_color=str(
                    v.get("exteriorColor") or v.get("color") or "N/A"
                ),
                dealership_domain=domain,
            )
        )

    inv_types = filters.get("inventoryTypes") or []
    total_count = next(
        (
            t.get("count")
            for t in inv_types
            if t.get("inventoryType") == condition.lower()
        ),
        len(raw_vehicles),
    )
    payment = filters.get("paymentFilters") or {}

    meta = {
        "totalInventoryCount": total_count,
        "modelBreakdown": [
            {"model": m["value"], "count": m.get("count", 0)} for m in _safe("models")
        ],
        "yearBreakdown": [
            {"year": y["value"], "count": y.get("count", 0)} for y in _safe("years")
        ],
        "colorBreakdown": [
            {"color": c.get("text") or c.get("value"), "count": c.get("count", 0)}
            for c in _safe("colors")
        ][:15],
        "trimBreakdown": [
            {"model": t.get("model"), "trim": t["value"], "count": t.get("count", 0)}
            for t in _safe("trims")
        ][:20],
        "fuelTypeBreakdown": [
            {"type": f["value"], "count": f.get("count", 0)} for f in _safe("fuelTypes")
        ],
        "locationBreakdown": [
            {"location": l["value"], "count": l.get("count", 0)}
            for l in _safe("locations")
        ],
        "priceRange": {
            "min": payment.get("dealerMinPayment", 0),
            "max": payment.get("dealerMaxPayment", 0),
        },
        "inStock": (filters.get("inStock") or {}).get("count", 0),
        "inTransit": (filters.get("inTransit") or {}).get("count", 0),
        "inProduction": (filters.get("inProduction") or {}).get("count", 0),
    }
    return vehicles, meta


# ---------------------------------------------------------------------------
# Per-domain pipeline
# ---------------------------------------------------------------------------


def scrape_domain(domain: str, condition: str) -> DealershipScrape:
    scrape = DealershipScrape(domain=domain, table_name=domain_to_table_name(domain))
    try:
        log.info("[%s] fetching homepage", domain)
        html = fetch_homepage(domain)
        platform, site_id = detect_platform(html, domain)
        scrape.platform = platform
        scrape.site_id = site_id
        log.info("[%s] detected %s, siteId=%s", domain, platform, site_id)

        if platform == "dealer_com":
            resp = fetch_dealer_com(domain, site_id, condition)
            vehicles, total = normalize_dealer_com(resp, domain, condition)
            scrape.vehicles = vehicles
            scrape.dc_total_count = total
            log.info(
                "[%s] got %d vehicles (reported total: %d)",
                domain, len(vehicles), total,
            )
        else:  # team_velocity
            resp = fetch_team_velocity(domain, condition)
            vehicles, meta = normalize_team_velocity(resp, domain, condition)
            scrape.vehicles = vehicles
            scrape.tv_meta = meta
            log.info(
                "[%s] got %d vehicles (reported total: %s)",
                domain, len(vehicles), meta.get("totalInventoryCount"),
            )
    except Exception as e:
        log.error("[%s] FAILED: %s", domain, e)
        scrape.error = str(e)
    return scrape


# ---------------------------------------------------------------------------
# Optional SQLite cache
# ---------------------------------------------------------------------------

_TABLE_NAME_RE = re.compile(r"^cars_[a-z0-9_]+$")


def _ensure_table(conn: sqlite3.Connection, table_name: str) -> None:
    # Defense in depth — table names come from domain_to_table_name() which
    # already strips unsafe chars, but validate before interpolating.
    if not _TABLE_NAME_RE.fullmatch(table_name):
        raise ValueError(f"Invalid table name: {table_name}")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            vin TEXT PRIMARY KEY,
            make TEXT,
            model TEXT,
            trim TEXT,
            year INTEGER,
            condition TEXT,
            price TEXT,
            odometer TEXT,
            exterior_color TEXT,
            dealership_domain TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )


def write_to_cache(cache_path: str, scrapes: list[DealershipScrape]) -> None:
    log.info("writing %d dealership(s) to cache: %s", len(scrapes), cache_path)
    with sqlite3.connect(cache_path) as conn:
        for s in scrapes:
            if s.error or not s.vehicles:
                continue
            _ensure_table(conn, s.table_name)
            conn.executemany(
                f"""INSERT OR IGNORE INTO {s.table_name}
                    (vin, make, model, trim, year, condition, price,
                     odometer, exterior_color, dealership_domain)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        v.vin, v.make, v.model, v.trim, v.year, v.condition,
                        v.price, v.odometer, v.exterior_color, v.dealership_domain,
                    )
                    for v in s.vehicles
                ],
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------


def _to_float_list(values, predicate=lambda x: True) -> list[float]:
    out = []
    for x in values:
        try:
            f = float(x)
            if predicate(f):
                out.append(f)
        except (TypeError, ValueError):
            pass
    return out


def aggregate(scrapes: list[DealershipScrape]) -> dict:
    """Compute per-dealership stats + cross-shopping matches."""
    inventory_by_dealer: dict[str, list[Vehicle]] = {
        s.domain: s.vehicles for s in scrapes if not s.error and s.vehicles
    }

    dealership_stats: dict[str, dict] = {}

    for s in scrapes:
        if s.error or not s.vehicles:
            continue
        vs = s.vehicles
        prices = _to_float_list((v.price for v in vs), lambda f: f > 0)
        odos = _to_float_list((v.odometer for v in vs), lambda f: f >= 0)
        years = _to_float_list((v.year for v in vs), lambda f: f > 1900)

        by_make: dict[str, int] = {}
        by_model: dict[str, int] = {}
        by_color: dict[str, int] = {}
        by_year: dict[int, int] = {}
        for v in vs:
            by_make[v.make] = by_make.get(v.make, 0) + 1
            by_model[v.model] = by_model.get(v.model, 0) + 1
            if v.exterior_color and v.exterior_color != "N/A":
                by_color[v.exterior_color] = by_color.get(v.exterior_color, 0) + 1
            if v.year:
                by_year[v.year] = by_year.get(v.year, 0) + 1

        # Prefer Team Velocity API metadata (richer + true totals) when available
        if (
            s.platform == "team_velocity"
            and s.tv_meta
            and s.tv_meta.get("totalInventoryCount")
        ):
            meta = s.tv_meta
            pr = meta.get("priceRange") or {}
            if pr.get("min") or pr.get("max"):
                price_stats = {
                    "min": pr["min"],
                    "max": pr["max"],
                    "avg": (pr["min"] + pr["max"]) // 2,
                }
            elif prices:
                price_stats = {
                    "min": min(prices),
                    "max": max(prices),
                    "avg": round(sum(prices) / len(prices)),
                }
            else:
                price_stats = None

            dealership_stats[s.domain] = {
                "platform": "team_velocity",
                "total_vehicles": meta["totalInventoryCount"],
                "returned_vehicles": len(vs),
                "price_stats": price_stats,
                "by_model": {m["model"]: m["count"] for m in meta["modelBreakdown"]},
                "by_year": {str(y["year"]): y["count"] for y in meta["yearBreakdown"]},
                "by_color": {c["color"]: c["count"] for c in meta["colorBreakdown"]},
                "by_fuel_type": {
                    f["type"]: f["count"] for f in meta["fuelTypeBreakdown"]
                },
                "by_trim": meta["trimBreakdown"],
                "inventory_status": {
                    "in_stock": meta["inStock"],
                    "in_transit": meta["inTransit"],
                    "in_production": meta["inProduction"],
                },
                "location_breakdown": meta["locationBreakdown"],
            }
        else:
            total = s.dc_total_count or len(vs)
            dealership_stats[s.domain] = {
                "platform": s.platform or "dealer_com",
                "total_vehicles": total,
                "returned_vehicles": len(vs),
                "data_complete": len(vs) >= total,
                "price_stats": (
                    {
                        "min": min(prices),
                        "max": max(prices),
                        "avg": round(sum(prices) / len(prices)),
                        "median": median(prices),
                    }
                    if prices
                    else None
                ),
                "odometer_stats": (
                    {
                        "min": min(odos),
                        "max": max(odos),
                        "avg": round(sum(odos) / len(odos)),
                    }
                    if odos
                    else None
                ),
                "year_stats": (
                    {"oldest": int(min(years)), "newest": int(max(years))}
                    if years
                    else None
                ),
                "by_make": by_make,
                "by_model": by_model,
                "by_color": by_color,
                "by_year": by_year,
            }

    # Cross-dealership matches: same {year} {make} {model} at 2+ dealerships
    fingerprints: dict[str, list[dict]] = {}
    for domain, vs in inventory_by_dealer.items():
        for v in vs:
            fp = f"{v.year} {v.make} {v.model}"
            fingerprints.setdefault(fp, []).append(
                {
                    "dealership": domain,
                    "price": v.price,
                    "odometer": v.odometer,
                    "trim": v.trim,
                    "vin": v.vin,
                }
            )

    common_vehicles = {
        fp: listings
        for fp, listings in fingerprints.items()
        if len({l["dealership"] for l in listings}) > 1
    }

    total_vehicles = sum(
        s.get("total_vehicles", 0) for s in dealership_stats.values()
    )

    return {
        "summary": {
            "total_count": total_vehicles,
            "dealerships_processed": len(dealership_stats),
        },
        "dealership_stats": dealership_stats,
        "common_vehicles": common_vehicles,
    }


# ---------------------------------------------------------------------------
# Prompt building + Claude call
# ---------------------------------------------------------------------------


def _fmt_money(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_miles(x) -> str:
    try:
        return f"{float(x):,.0f} mi"
    except (TypeError, ValueError):
        return "N/A"


def build_prompt(data: dict) -> str:
    out: list[str] = [
        "You are an automotive inventory analyst. Analyze the following dealership "
        "inventory data and create a comprehensive comparison report.",
        "",
        "## INVENTORY DATA SUMMARY",
        "",
        f"### Dealerships Analyzed: {len(data['dealership_stats'])}",
        f"### Total Vehicles: {data['summary']['total_count']}",
        "",
        "## DEALERSHIP STATISTICS",
        "",
    ]

    for dealer, stats in data["dealership_stats"].items():
        out.append(f"### {dealer}")
        out.append(f"- Total Vehicles: {stats['total_vehicles']}")

        ps = stats.get("price_stats")
        if ps:
            out.append(
                f"- Price Range: {_fmt_money(ps['min'])} - {_fmt_money(ps['max'])}"
            )
            out.append(f"- Average Price: {_fmt_money(ps['avg'])}")
            if ps.get("median") is not None:
                out.append(f"- Median Price: {_fmt_money(ps['median'])}")

        os_ = stats.get("odometer_stats")
        if os_:
            out.append(
                f"- Mileage Range: {int(os_['min']):,} - {int(os_['max']):,} miles"
            )
            out.append(f"- Average Mileage: {int(os_['avg']):,} miles")

        ys = stats.get("year_stats")
        if ys:
            out.append(f"- Year Range: {ys['oldest']} - {ys['newest']}")

        if stats.get("by_make"):
            out.append("\n**Inventory by Make:**")
            for make, count in sorted(stats["by_make"].items(), key=lambda x: -x[1]):
                out.append(f"  - {make}: {count} vehicles")

        if stats.get("by_model"):
            out.append("\n**Top Models:**")
            for model_, count in sorted(
                stats["by_model"].items(), key=lambda x: -x[1]
            )[:10]:
                out.append(f"  - {model_}: {count} vehicles")

        if stats.get("by_year"):
            out.append("\n**Inventory by Year:**")
            for year, count in sorted(
                stats["by_year"].items(), key=lambda x: str(x[0]), reverse=True
            ):
                out.append(f"  - {year}: {count} vehicles")

        if stats.get("by_color"):
            out.append("\n**Popular Colors:**")
            for color, count in sorted(
                stats["by_color"].items(), key=lambda x: -x[1]
            )[:5]:
                out.append(f"  - {color}: {count} vehicles")

        out.append("\n---\n")

    common = data.get("common_vehicles", {})
    if common:
        out.append(
            f"## VEHICLES AVAILABLE AT MULTIPLE DEALERSHIPS ({len(common)} matches)\n"
        )
        for fp, listings in sorted(common.items(), key=lambda x: -len(x[1]))[:20]:
            out.append(f"### {fp}")
            for l in listings:
                out.append(
                    f"  - {l['dealership']}: {_fmt_money(l['price'])}, "
                    f"{_fmt_miles(l['odometer'])}, {l.get('trim') or 'Base'}"
                )
            out.append("")

    out += [
        "",
        "## YOUR TASK",
        "",
        "Based on the data above, please create a comprehensive Inventory "
        "Comparison Report that includes:",
        "",
        "1. **Executive Summary**: Brief overview of all dealerships and key findings",
        "",
        "2. **Competitive Analysis**:",
        "   - Which dealership has the most competitive pricing?",
        "   - Which has the largest/freshest inventory?",
        "   - Price positioning comparison",
        "",
        "3. **Market Insights**:",
        "   - Most popular makes/models across all dealerships",
        "   - Pricing trends",
        "   - Inventory age analysis",
        "",
        "4. **Cross-Shopping Opportunities**:",
        "   - Highlight vehicles available at multiple dealerships with price differences",
        "   - Best deals identified",
        "",
        "5. **Recommendations**:",
        "   - For buyers: where to find the best value",
        "   - For dealerships: competitive positioning suggestions",
        "",
        "Provide specific numbers and actionable insights. Format the report in a "
        "clear, professional manner.",
    ]
    return "\n".join(out)


def generate_report(prompt: str, model: str = MODEL, max_tokens: int = MAX_TOKENS) -> str:
    client = Anthropic(api_key="REDACTED")
    log.info("calling Claude (%s, max_tokens=%d)...", model, max_tokens)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in msg.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape dealership inventories (Dealer.com / Team Velocity) and "
            "generate a Claude-written comparison report."
        ),
    )
    parser.add_argument(
        "domains",
        nargs="+",
        help="Dealership domains, e.g. smythevolvocars.com volvocarsprinceton.com",
    )
    parser.add_argument(
        "--condition",
        choices=["new", "used"],
        default="new",
        help="Inventory condition (default: new)",
    )
    parser.add_argument(
        "--cache",
        metavar="PATH",
        help=(
            "Optional SQLite file to mirror per-dealership tables "
            "(default: in-memory only)"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


    log.info(
        "scraping %d domain(s) with condition=%s", len(args.domains), args.condition
    )
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        scrapes = list(
            ex.map(lambda d: scrape_domain(d, args.condition), args.domains)
        )

    successes = [s for s in scrapes if not s.error]
    failures = [s for s in scrapes if s.error]
    if failures:
        log.warning("%d/%d dealerships failed", len(failures), len(scrapes))
    if not successes:
        log.error("no dealerships scraped successfully; nothing to analyze.")
        return 1

    if args.cache:
        try:
            write_to_cache(args.cache, successes)
        except Exception as e:
            log.error("failed to write cache: %s", e)

    data = aggregate(successes)
    prompt = build_prompt(data)
    log.info("prompt is %d chars", len(prompt))

    try:
        report = generate_report(prompt)
    except Exception as e:
        log.error("Claude call failed: %s", e)
        return 1

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
