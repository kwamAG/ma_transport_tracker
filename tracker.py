#!/usr/bin/env python3
"""MA Transportation Opportunity Tracker.

Finds NEMT, courier, paratransit, shuttle, freight, rideshare, last-mile
delivery, and diversified transport opportunities in Massachusetts from both
government (SAM.gov, COMMBUYS) and private sector sources (Craigslist, Indeed,
curated directory). Generates a mobile-friendly HTML report and CSV export.
Uses only Python standard library.
"""

import json
import os
import csv
import ssl
import re
import hashlib
import time
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
SEEN_PATH = os.path.join(SCRIPT_DIR, "seen_opportunities.json")
MANUAL_PATH = os.path.join(SCRIPT_DIR, "manual_opportunities.json")
OUTPUT_HTML = os.path.join(SCRIPT_DIR, "docs", "index.html")
OUTPUT_CSV = os.path.join(SCRIPT_DIR, "docs", "opportunities.csv")

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def load_json(path):
    """Load and return parsed JSON from *path*."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    """Write *data* as formatted JSON to *path*."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def match_keywords(text, keywords):
    """Return list of *keywords* found in *text* (case-insensitive)."""
    if not text:
        return []
    lower = text.lower()
    return [kw for kw in keywords if kw.lower() in lower]


def contains_excluded(text, exclude_kw):
    """Return True if any exclude keyword is found in *text*."""
    if not text:
        return False
    lower = text.lower()
    return any(kw.lower() in lower for kw in exclude_kw)


def escape_html(text):
    """Escape HTML special characters."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def format_currency(val):
    """Format a number as a short currency string."""
    if not val:
        return "N/A"
    try:
        val = float(val)
    except (ValueError, TypeError):
        return "N/A"
    if val >= 1_000_000:
        return "${:.1f}M".format(val / 1_000_000)
    if val >= 1_000:
        return "${:.0f}K".format(val / 1_000)
    return "${:,.0f}".format(val)


def format_date_display(date_str):
    """Format a date string for display (YYYY-MM-DD or ISO)."""
    if not date_str:
        return ""
    s = str(date_str).strip()
    if "T" in s:
        s = s.split("T")[0]
    # Handle MM/dd/yyyy from SAM.gov
    if "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            try:
                return "{}-{}-{}".format(parts[2], parts[0].zfill(2), parts[1].zfill(2))
            except Exception:
                pass
    return s


def strip_html_tags(text):
    """Remove HTML tags from text, returning plain text."""
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', ' ', str(text))
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


def stable_id(prefix, value):
    """Generate a stable ID from a prefix and value using MD5 hash."""
    h = hashlib.md5(str(value).encode("utf-8")).hexdigest()[:12]
    return "{}-{}".format(prefix, h)


# ---------------------------------------------------------------------------
# SAM.gov API integration
# ---------------------------------------------------------------------------

def api_fetch_sam(config, naics_code, offset=0, limit=25):
    """Query SAM.gov opportunities API for a single NAICS code + state."""
    api_key = config.get("sam_api_key", "")
    base_url = config.get("sam_api_base_url", "https://api.sam.gov/opportunities/v2/search")
    days_back = config.get("search_days_back", 365)

    now = datetime.now(timezone.utc)
    posted_from = (now - timedelta(days=days_back)).strftime("%m/%d/%Y")
    posted_to = now.strftime("%m/%d/%Y")

    params = urllib.parse.urlencode({
        "api_key": api_key,
        "postedFrom": posted_from,
        "postedTo": posted_to,
        "ncode": naics_code,
        "limit": limit,
        "offset": offset,
        "ptype": "o,p,k",
    })

    for state in config.get("states", ["MA"]):
        params += "&state=" + urllib.parse.quote(state)

    url = "{}?{}".format(base_url, params)
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "MATransportTracker/2.0"})

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data
    except Exception as e:
        print("  SAM.gov API error (NAICS {}): {}".format(naics_code, e))
        return None


def fetch_all_sam_opportunities(config):
    """Fetch opportunities from SAM.gov across all configured NAICS codes."""
    api_key = config.get("sam_api_key", "")
    if not api_key:
        print("  WARNING: No SAM.gov API key configured. Skipping SAM.gov fetch.")
        print("  Set 'sam_api_key' in config.json to enable SAM.gov search.")
        return []

    naics_codes = config.get("naics_codes", [])
    seen_ids = set()
    all_opportunities = []

    for naics in naics_codes:
        print("  Fetching NAICS {}...".format(naics))
        offset = 0
        limit = 25
        max_pages = 40

        for page in range(max_pages):
            data = api_fetch_sam(config, naics, offset=offset, limit=limit)
            if not data:
                break

            opps = data.get("opportunitiesData", [])
            if not opps:
                break

            for opp in opps:
                notice_id = opp.get("noticeId", "")
                if notice_id and notice_id not in seen_ids:
                    seen_ids.add(notice_id)
                    all_opportunities.append(opp)

            total = data.get("totalRecords", 0)
            offset += limit
            if offset >= total:
                break

        print("    Found {} unique opportunities so far".format(len(all_opportunities)))

    return all_opportunities


# ---------------------------------------------------------------------------
# Craigslist RSS integration
# ---------------------------------------------------------------------------

def fetch_craigslist_opportunities(config):
    """Fetch transport job postings from Craigslist RSS feeds.

    Reads feed definitions from config, parses RSS/RDF XML, filters by
    transport keywords, and returns standardized opportunity dicts.
    """
    feeds = config.get("craigslist_feeds", [])
    if not feeds:
        print("  No Craigslist feeds configured.")
        return []

    direct_kw = config.get("direct_transport_keywords", [])
    service_kw = config.get("service_type_keywords", [])
    private_kw = config.get("private_sector_keywords", [])
    exclude_kw = config.get("exclude_keywords", [])
    all_keywords = direct_kw + service_kw + private_kw

    ctx = ssl.create_default_context()
    opportunities = []
    seen_links = set()

    # Common RSS/RDF namespaces
    namespaces = {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "dc": "http://purl.org/dc/elements/1.1/",
        "content": "http://purl.org/rss/1.0/modules/content/",
    }

    for feed in feeds:
        feed_name = feed.get("name", "Unknown")
        feed_url = feed.get("url", "")
        if not feed_url:
            continue

        print("  Fetching Craigslist feed: {}...".format(feed_name))
        try:
            req = urllib.request.Request(feed_url, headers={
                "User-Agent": "MATransportTracker/2.0",
                "Accept": "application/rss+xml, application/xml, text/xml",
            })
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                xml_bytes = resp.read()

            root = ET.fromstring(xml_bytes)

            # Handle both standard RSS and RDF formats
            items = root.findall(".//item")
            if not items:
                items = root.findall(".//{http://purl.org/rss/1.0/}item")

            feed_count = 0
            for item in items:
                # Extract title
                title_el = item.find("title")
                if title_el is None:
                    title_el = item.find("{http://purl.org/rss/1.0/}title")
                title = title_el.text if title_el is not None and title_el.text else ""

                # Extract link
                link_el = item.find("link")
                if link_el is None:
                    link_el = item.find("{http://purl.org/rss/1.0/}link")
                link = link_el.text if link_el is not None and link_el.text else ""
                if not link:
                    link = item.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", "")

                if not link or link in seen_links:
                    continue
                seen_links.add(link)

                # Extract description
                desc_el = item.find("description")
                if desc_el is None:
                    desc_el = item.find("{http://purl.org/rss/1.0/}description")
                raw_desc = desc_el.text if desc_el is not None and desc_el.text else ""
                description = strip_html_tags(raw_desc)

                # Extract date
                date_el = item.find("{http://purl.org/dc/elements/1.1/}date")
                if date_el is None:
                    date_el = item.find("pubDate")
                date_str = date_el.text if date_el is not None and date_el.text else ""

                # Keyword matching
                search_text = "{} {}".format(title, description)
                if contains_excluded(search_text, exclude_kw):
                    continue

                matched = match_keywords(search_text, all_keywords)
                if not matched:
                    continue

                opp_id = stable_id("cl", link)
                service_type = classify_service_type(search_text, matched)

                matched_direct = match_keywords(search_text, direct_kw)
                relevance = "high" if matched_direct else "medium"

                opportunities.append({
                    "id": opp_id,
                    "title": title[:200],
                    "solicitation_number": "",
                    "agency": "Craigslist - {}".format(feed.get("category", "general").title()),
                    "posted_date": format_date_display(date_str),
                    "response_deadline": "",
                    "naics_code": "",
                    "award_amount": 0,
                    "place_of_performance": "Boston Area, MA",
                    "description": description[:500],
                    "contact_name": "",
                    "contact_email": "",
                    "contact_phone": "",
                    "url": link,
                    "keywords_matched": matched,
                    "relevance": relevance,
                    "service_type": service_type,
                    "source": "Craigslist",
                    "sector": "private",
                    "opportunity_type": "job_posting",
                    "status": "active",
                    "is_new": False,
                    "notes": "Found via Craigslist RSS feed: {}".format(feed_name),
                })
                feed_count += 1

            print("    Found {} matching postings".format(feed_count))

        except Exception as e:
            print("    Error fetching {}: {}".format(feed_name, e))

        time.sleep(2)

    return opportunities


# ---------------------------------------------------------------------------
# Indeed RSS integration
# ---------------------------------------------------------------------------

def fetch_indeed_opportunities(config):
    """Fetch transport job postings from Indeed RSS feeds.

    Designed to fail gracefully -- Indeed RSS is unreliable and may return
    403 or redirect to HTML. Returns [] if unavailable.
    """
    feeds = config.get("indeed_feeds", [])
    if not feeds:
        print("  No Indeed feeds configured.")
        return []

    direct_kw = config.get("direct_transport_keywords", [])
    service_kw = config.get("service_type_keywords", [])
    private_kw = config.get("private_sector_keywords", [])
    exclude_kw = config.get("exclude_keywords", [])
    all_keywords = direct_kw + service_kw + private_kw

    ctx = ssl.create_default_context()
    opportunities = []
    seen_links = set()

    for feed in feeds:
        feed_name = feed.get("name", "Unknown")
        feed_url = feed.get("url", "")
        if not feed_url:
            continue

        print("  Fetching Indeed feed: {}...".format(feed_name))
        try:
            req = urllib.request.Request(feed_url, headers={
                "User-Agent": "MATransportTracker/2.0",
                "Accept": "application/rss+xml, application/xml, text/xml",
            })
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                content_type = resp.headers.get("Content-Type", "")
                # Indeed may redirect to HTML -- check Content-Type
                if "html" in content_type.lower() and "xml" not in content_type.lower():
                    print("    Indeed returned HTML (not RSS), skipping feed")
                    continue
                xml_bytes = resp.read()

            root = ET.fromstring(xml_bytes)
            items = root.findall(".//item")

            feed_count = 0
            for item in items:
                title_el = item.find("title")
                title = title_el.text if title_el is not None and title_el.text else ""

                link_el = item.find("link")
                link = link_el.text if link_el is not None and link_el.text else ""
                if not link or link in seen_links:
                    continue
                seen_links.add(link)

                desc_el = item.find("description")
                raw_desc = desc_el.text if desc_el is not None and desc_el.text else ""
                description = strip_html_tags(raw_desc)

                date_el = item.find("pubDate")
                date_str = date_el.text if date_el is not None and date_el.text else ""

                # Keyword matching
                search_text = "{} {}".format(title, description)
                if contains_excluded(search_text, exclude_kw):
                    continue

                matched = match_keywords(search_text, all_keywords)
                if not matched:
                    continue

                opp_id = stable_id("indeed", link)
                service_type = classify_service_type(search_text, matched)

                matched_direct = match_keywords(search_text, direct_kw)
                relevance = "high" if matched_direct else "medium"

                opportunities.append({
                    "id": opp_id,
                    "title": title[:200],
                    "solicitation_number": "",
                    "agency": "Indeed - {}".format(feed.get("category", "general").title()),
                    "posted_date": format_date_display(date_str),
                    "response_deadline": "",
                    "naics_code": "",
                    "award_amount": 0,
                    "place_of_performance": "Massachusetts",
                    "description": description[:500],
                    "contact_name": "",
                    "contact_email": "",
                    "contact_phone": "",
                    "url": link,
                    "keywords_matched": matched,
                    "relevance": relevance,
                    "service_type": service_type,
                    "source": "Indeed",
                    "sector": "private",
                    "opportunity_type": "job_posting",
                    "status": "active",
                    "is_new": False,
                    "notes": "Found via Indeed RSS feed: {}".format(feed_name),
                })
                feed_count += 1

            print("    Found {} matching postings".format(feed_count))

        except Exception as e:
            print("    Error fetching {} (expected - Indeed RSS is unreliable): {}".format(feed_name, e))

        time.sleep(2)

    return opportunities


# ---------------------------------------------------------------------------
# Private directory checks
# ---------------------------------------------------------------------------

def check_directory_entries(config):
    """Check reachability of private directory entries and return as opportunities.

    Performs HTTP HEAD request to each URL to verify reachability.
    Returns standardized opportunity dicts.
    """
    directory = config.get("private_directory", [])
    if not directory:
        print("  No private directory entries configured.")
        return []

    ctx = ssl.create_default_context()
    opportunities = []

    for entry in directory:
        name = entry.get("name", "Unknown")
        url = entry.get("url", "")
        if not url:
            continue

        # Check reachability via HEAD request
        status = "active"
        print("  Checking directory: {}...".format(name))
        try:
            req = urllib.request.Request(url, method="HEAD", headers={
                "User-Agent": "MATransportTracker/2.0",
            })
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                if resp.status < 400:
                    status = "active"
                else:
                    status = "unverified"
        except Exception:
            status = "unverified"

        print("    Status: {}".format(status))

        opp_id = stable_id("dir", url)
        category = entry.get("category", "Other")
        description = entry.get("description", "")
        requirements = entry.get("requirements", "")
        earning = entry.get("earning_potential", "")

        full_desc = description
        if requirements:
            full_desc += " | Requirements: " + requirements
        if earning:
            full_desc += " | Earning Potential: " + earning

        # Classify service type based on category
        category_to_service = {
            "Last-Mile": "Last-Mile Delivery",
            "Freight": "Freight",
            "Rideshare/Gig": "Rideshare/Gig",
            "NEMT": "NEMT",
        }
        service_type = category_to_service.get(category, "Other Transport")

        # Determine opportunity type from category
        category_to_opp_type = {
            "Last-Mile": "partnership",
            "Freight": "contract",
            "Rideshare/Gig": "gig",
            "NEMT": "contract",
        }
        opp_type = category_to_opp_type.get(category, "partnership")

        opportunities.append({
            "id": opp_id,
            "title": name,
            "solicitation_number": "",
            "agency": name,
            "posted_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "response_deadline": "",
            "naics_code": "",
            "award_amount": 0,
            "place_of_performance": "Massachusetts",
            "description": full_desc,
            "contact_name": "",
            "contact_email": "",
            "contact_phone": "",
            "url": url,
            "keywords_matched": [category.lower()],
            "relevance": "medium",
            "service_type": service_type,
            "source": "Directory",
            "sector": "private",
            "opportunity_type": opp_type,
            "status": status,
            "is_new": False,
            "notes": "Category: {} | Earning: {}".format(category, earning),
        })

    return opportunities


# ---------------------------------------------------------------------------
# Processing and scoring
# ---------------------------------------------------------------------------

def score_relevance(matched_direct, matched_service, award_amount, auto_high_value):
    """Score relevance: 'high', 'medium', or 'low'."""
    try:
        amount = float(award_amount) if award_amount else 0
    except (ValueError, TypeError):
        amount = 0

    if matched_direct:
        return "high"
    if amount >= auto_high_value:
        return "high"
    if matched_service:
        return "medium"
    return "low"


def classify_service_type(text, matched_keywords):
    """Classify opportunity into a service type category.

    Returns one of: NEMT, Courier/Delivery, Paratransit, Shuttle/Charter,
    Logistics, Freight, Rideshare/Gig, Last-Mile Delivery, Other Transport
    """
    if not text:
        text = ""
    lower = text.lower()
    kw_str = " ".join(matched_keywords).lower() if matched_keywords else ""
    combined = lower + " " + kw_str

    nemt_terms = ["nemt", "non-emergency medical", "medical transport",
                  "patient transport", "medicaid transport", "medical transportation",
                  "nemt provider", "modivcare", "mtm", "veyo"]
    if any(t in combined for t in nemt_terms):
        return "NEMT"

    para_terms = ["paratransit", "dial-a-ride", "wheelchair", "stretcher",
                  "ambulatory", "ada transport"]
    if any(t in combined for t in para_terms):
        return "Paratransit"

    freight_terms = ["freight", "trucking", "cdl", "ltl", "truckload",
                     "owner operator", "xpo", "uber freight", "fedex ground",
                     "box truck", "tractor trailer", "18 wheel"]
    if any(t in combined for t in freight_terms):
        return "Freight"

    rideshare_terms = ["rideshare", "ride-share", "uber", "lyft", "doordash",
                       "instacart", "grubhub", "gig", "food delivery",
                       "grocery delivery"]
    if any(t in combined for t in rideshare_terms):
        return "Rideshare/Gig"

    lastmile_terms = ["last-mile", "last mile", "amazon flex", "amazon dsp",
                      "delivery partner", "delivery service partner",
                      "parcel delivery", "package delivery", "sprinter van",
                      "cargo van", "delivery route"]
    if any(t in combined for t in lastmile_terms):
        return "Last-Mile Delivery"

    courier_terms = ["courier", "delivery", "specimen", "laboratory",
                     "pharmacy"]
    if any(t in combined for t in courier_terms):
        return "Courier/Delivery"

    shuttle_terms = ["shuttle", "airport", "charter", "passenger", "van service"]
    if any(t in combined for t in shuttle_terms):
        return "Shuttle/Charter"

    logistics_terms = ["logistics", "fleet", "ground transportation"]
    if any(t in combined for t in logistics_terms):
        return "Logistics"

    return "Other Transport"


def process_sam_opportunities(raw_opps, config):
    """Process raw SAM.gov results: filter exclusions, match keywords, score."""
    direct_kw = config.get("direct_transport_keywords", [])
    service_kw = config.get("service_type_keywords", [])
    exclude_kw = config.get("exclude_keywords", [])
    auto_high = config.get("auto_high_value", 500000)
    processed = []

    for opp in raw_opps:
        title = opp.get("title", "") or ""
        description = opp.get("description", "") or ""
        search_text = " ".join([
            title,
            description,
            opp.get("organizationName", "") or "",
            opp.get("placeOfPerformance", {}).get("state", {}).get("name", "") if isinstance(opp.get("placeOfPerformance"), dict) else "",
        ])

        if contains_excluded(search_text, exclude_kw):
            continue

        matched_direct = match_keywords(search_text, direct_kw)
        matched_service = match_keywords(search_text, service_kw)

        award_raw = opp.get("award", {})
        award_amount = 0
        if isinstance(award_raw, dict):
            award_amount = award_raw.get("amount", 0) or 0
        elif award_raw:
            try:
                award_amount = float(award_raw)
            except (ValueError, TypeError):
                award_amount = 0

        relevance = score_relevance(matched_direct, matched_service, award_amount, auto_high)
        all_matched = matched_direct + matched_service
        service_type = classify_service_type(search_text, all_matched)

        pop = opp.get("placeOfPerformance", {})
        pop_str = ""
        if isinstance(pop, dict):
            city = pop.get("city", {})
            state = pop.get("state", {})
            city_name = city.get("name", "") if isinstance(city, dict) else str(city)
            state_name = state.get("name", "") if isinstance(state, dict) else str(state)
            pop_str = ", ".join(filter(None, [city_name, state_name]))
        if not pop_str:
            pop_str = "Massachusetts"

        contact = opp.get("pointOfContact", [])
        contact_name = ""
        contact_email = ""
        contact_phone = ""
        if isinstance(contact, list) and contact:
            c = contact[0]
            contact_name = c.get("fullName", "") or ""
            contact_email = c.get("email", "") or ""
            contact_phone = c.get("phone", "") or ""
        elif isinstance(contact, dict):
            contact_name = contact.get("fullName", "") or ""
            contact_email = contact.get("email", "") or ""
            contact_phone = contact.get("phone", "") or ""

        notice_id = opp.get("noticeId", "")
        sol_number = opp.get("solicitationNumber", "") or ""

        processed.append({
            "id": notice_id,
            "title": title,
            "solicitation_number": sol_number,
            "agency": opp.get("organizationName", "") or opp.get("departmentName", "") or "N/A",
            "posted_date": opp.get("postedDate", "") or "",
            "response_deadline": opp.get("responseDeadLine", "") or "",
            "naics_code": opp.get("naicsCode", "") or "",
            "award_amount": award_amount,
            "place_of_performance": pop_str,
            "description": description,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "contact_phone": contact_phone,
            "url": "https://sam.gov/opp/{}/view".format(notice_id) if notice_id else "",
            "keywords_matched": all_matched,
            "relevance": relevance,
            "service_type": service_type,
            "source": "SAM.gov",
            "sector": "public",
            "opportunity_type": "contract",
            "status": "active",
            "is_new": False,
            "notes": "",
        })

    return processed


def process_manual_opportunities(entries, config):
    """Process manual opportunity entries through the same pipeline."""
    direct_kw = config.get("direct_transport_keywords", [])
    service_kw = config.get("service_type_keywords", [])
    exclude_kw = config.get("exclude_keywords", [])
    auto_high = config.get("auto_high_value", 500000)
    processed = []

    for entry in entries:
        search_text = " ".join([
            entry.get("title", ""),
            entry.get("description", ""),
            entry.get("agency", ""),
            entry.get("notes", ""),
        ])

        if contains_excluded(search_text, exclude_kw):
            continue

        matched_direct = match_keywords(search_text, direct_kw)
        matched_service = match_keywords(search_text, service_kw)
        all_matched = matched_direct + matched_service

        award_amount = entry.get("award_amount", 0) or 0
        relevance = score_relevance(matched_direct, matched_service, award_amount, auto_high)
        service_type = classify_service_type(search_text, all_matched)

        processed.append({
            "id": entry.get("id", ""),
            "title": entry.get("title", ""),
            "solicitation_number": "",
            "agency": entry.get("agency", "N/A"),
            "posted_date": entry.get("posted_date", ""),
            "response_deadline": entry.get("response_deadline", ""),
            "naics_code": entry.get("naics_code", ""),
            "award_amount": award_amount,
            "place_of_performance": entry.get("place_of_performance", "Massachusetts"),
            "description": entry.get("description", ""),
            "contact_name": entry.get("contact_name", ""),
            "contact_email": entry.get("contact_email", ""),
            "contact_phone": entry.get("contact_phone", ""),
            "url": entry.get("url", ""),
            "keywords_matched": all_matched,
            "relevance": relevance,
            "service_type": service_type,
            "source": "Manual",
            "sector": entry.get("sector", "public"),
            "opportunity_type": entry.get("opportunity_type", "contract"),
            "status": entry.get("status", "active"),
            "is_new": False,
            "notes": entry.get("notes", ""),
        })

    return processed


def identify_new_opportunities(opps, seen_ids):
    """Mark opportunities as new if their id is not in *seen_ids*."""
    for opp in opps:
        opp["is_new"] = opp["id"] not in seen_ids
    return opps


def get_commbuys_search_links(config):
    """Generate COMMBUYS search links for key transport terms."""
    base_url = config.get("commbuys_search_url",
                          "https://www.commbuys.com/bso/external/publicBids.sdo")
    terms = [
        ("NEMT / Medical Transport", "non-emergency medical transportation"),
        ("Paratransit", "paratransit transportation"),
        ("Courier / Delivery", "courier delivery services"),
        ("Shuttle Services", "shuttle transportation"),
        ("Transportation Services", "transportation services"),
        ("Patient Transport", "patient transport"),
        ("Wheelchair Van", "wheelchair van service"),
        ("Fleet Services", "fleet management transportation"),
    ]
    links = []
    for label, query in terms:
        url = "{}?{}".format(base_url, urllib.parse.urlencode({"keywords": query}))
        links.append((label, url))
    return links


# ---------------------------------------------------------------------------
# CSV generation
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "id", "title", "solicitation_number", "agency", "posted_date",
    "response_deadline", "naics_code", "award_amount", "place_of_performance",
    "description", "contact_name", "contact_email", "contact_phone", "url",
    "keywords_matched", "relevance", "service_type", "source", "sector",
    "opportunity_type", "status", "is_new", "notes",
]


def generate_csv(all_opportunities, output_path):
    """Write all opportunities to a CSV file with BOM for Excel compatibility."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for opp in all_opportunities:
            row = dict(opp)
            if isinstance(row.get("keywords_matched"), list):
                row["keywords_matched"] = "; ".join(row["keywords_matched"])
            writer.writerow(row)


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(all_opportunities, commbuys_links, directory_entries, run_time):
    """Generate mobile-friendly HTML report with search, filters, sector badges,
    and private sector directory section."""

    # Sort: high relevance first, then by award amount descending
    def sort_key(opp):
        rel_order = {"high": 0, "medium": 1, "low": 2}
        try:
            amount = float(opp.get("award_amount", 0) or 0)
        except (ValueError, TypeError):
            amount = 0
        return (rel_order.get(opp.get("relevance", "low"), 2), -amount)

    all_opportunities.sort(key=sort_key)

    # Summary stats
    summary_new = sum(1 for o in all_opportunities if o.get("is_new"))
    summary_total = len(all_opportunities)
    public_count = sum(1 for o in all_opportunities if o.get("sector") == "public")
    private_count = sum(1 for o in all_opportunities if o.get("sector") == "private")
    sam_count = sum(1 for o in all_opportunities if o.get("source") == "SAM.gov")
    feed_count = sum(1 for o in all_opportunities if o.get("source") in ("Craigslist", "Indeed"))
    dir_count = sum(1 for o in all_opportunities if o.get("source") == "Directory")
    high_count = sum(1 for o in all_opportunities if o.get("relevance") == "high")

    # Collect service types and statuses for filter dropdowns
    service_types = sorted(set(o.get("service_type", "Other Transport") for o in all_opportunities))
    statuses = sorted(set(o.get("status", "active") for o in all_opportunities))

    # COMMBUYS links HTML
    cb_links_html = ""
    for label, url in commbuys_links:
        cb_links_html += '<a class="link-btn cb-link" href="{}" target="_blank" rel="noopener">{}</a>\n'.format(
            escape_html(url), escape_html(label))

    # --- Private Directory Section HTML ---
    dir_section_html = ""
    if directory_entries:
        # Group by category
        categories = {}
        for entry in directory_entries:
            cat = entry.get("service_type", "Other")
            categories.setdefault(cat, []).append(entry)

        dir_cards = ""
        for cat_name in ["Last-Mile Delivery", "Freight", "Rideshare/Gig", "NEMT", "Other Transport"]:
            if cat_name not in categories:
                continue
            dir_cards += '<h4 class="dir-cat-title">{}</h4>'.format(escape_html(cat_name))
            dir_cards += '<div class="dir-cat-grid">'
            for entry in categories[cat_name]:
                status_dot = "green" if entry.get("status") == "active" else "red"
                status_label = "Reachable" if entry.get("status") == "active" else "Unverified"
                earning = ""
                notes = entry.get("notes", "")
                if "Earning:" in notes:
                    earning = notes.split("Earning:")[1].strip()

                dir_cards += (
                    '<div class="dir-card">'
                    '<div class="dir-card-header">'
                    '<strong>{name}</strong>'
                    '<span class="dir-status" style="color:{dot_color};">'
                    '&#9679; {status_label}</span>'
                    '</div>'
                    '<div class="dir-card-desc">{desc}</div>'
                    '{earning_html}'
                    '<div class="dir-card-links">'
                    '<a class="link-btn dir-link" href="{url}" target="_blank" '
                    'rel="noopener">Apply / Learn More</a>'
                    '</div>'
                    '</div>'
                ).format(
                    name=escape_html(entry.get("title", "")),
                    dot_color=status_dot,
                    status_label=status_label,
                    desc=escape_html(str(entry.get("description", ""))[:200]),
                    earning_html='<div class="dir-card-earning"><strong>Earning Potential:</strong> {}</div>'.format(
                        escape_html(earning)) if earning else "",
                    url=escape_html(entry.get("url", "")),
                )
            dir_cards += '</div>'

        dir_section_html = (
            '<details class="directory-section">'
            '<summary>Private Sector Directory &mdash; {count} curated opportunities</summary>'
            '<div class="dir-body">{cards}</div>'
            '</details>'
        ).format(count=len(directory_entries), cards=dir_cards)

    # --- Render cards ---
    def render_card(opp):
        source = opp.get("source", "SAM.gov")
        sector = opp.get("sector", "public")
        opp_type = opp.get("opportunity_type", "contract")

        source_colors = {
            "SAM.gov": "#2980b9",
            "Manual": "#8e44ad",
            "Craigslist": "#e67e22",
            "Indeed": "#c0392b",
            "Directory": "#16a085",
        }
        source_color = source_colors.get(source, "#7f8c8d")

        # Sector badge
        sector_color = "#2980b9" if sector == "public" else "#e67e22"
        sector_label = "Public" if sector == "public" else "Private"

        rel = opp.get("relevance", "low")
        rel_colors = {"high": "#c0392b", "medium": "#e67e22", "low": "#7f8c8d"}
        rel_color = rel_colors.get(rel, "#7f8c8d")

        stype = opp.get("service_type", "Other Transport")
        stype_colors = {
            "NEMT": "#16a085",
            "Courier/Delivery": "#d35400",
            "Paratransit": "#2c3e50",
            "Shuttle/Charter": "#27ae60",
            "Logistics": "#8e44ad",
            "Freight": "#c0392b",
            "Rideshare/Gig": "#e67e22",
            "Last-Mile Delivery": "#2980b9",
            "Other Transport": "#7f8c8d",
        }
        stype_color = stype_colors.get(stype, "#7f8c8d")

        new_badge = ""
        if opp.get("is_new"):
            new_badge = '<span class="badge badge-new">NEW</span>'

        # Keywords tags
        kw_html = ""
        if opp.get("keywords_matched"):
            tags = "".join(
                '<span class="kw-tag">{}</span>'.format(escape_html(k))
                for k in sorted(set(opp["keywords_matched"]))
            )
            kw_html = '<div class="card-keywords">Keywords: {}</div>'.format(tags)

        # Dates
        posted = format_date_display(opp.get("posted_date", ""))
        deadline = format_date_display(opp.get("response_deadline", ""))

        # Award amount
        award = opp.get("award_amount", 0)
        award_display = format_currency(award) if award else "N/A"

        # Description (truncated)
        desc = str(opp.get("description") or "N/A")
        desc_short = desc[:300] + ("..." if len(desc) > 300 else "")

        # Contact info
        contact_parts = []
        if opp.get("contact_name"):
            contact_parts.append(escape_html(opp["contact_name"]))
        if opp.get("contact_email"):
            contact_parts.append('<a href="mailto:{e}">{e}</a>'.format(e=escape_html(opp["contact_email"])))
        if opp.get("contact_phone"):
            contact_parts.append(escape_html(opp["contact_phone"]))
        contact_html = ""
        if contact_parts:
            contact_html = '<div class="card-contact">{}</div>'.format(" &bull; ".join(contact_parts))

        # Links row -- context-sensitive
        links = []
        if opp.get("url"):
            if source == "SAM.gov":
                link_label = "View on SAM.gov"
            elif source == "Craigslist":
                link_label = "View on Craigslist"
            elif source == "Indeed":
                link_label = "View on Indeed"
            elif source == "Directory":
                link_label = "Apply / Learn More"
            else:
                link_label = "View Source"
            links.append('<a class="link-btn" href="{}" target="_blank" rel="noopener">{}</a>'.format(
                escape_html(opp["url"]), link_label))

        if sector == "public":
            # COMMBUYS search for public sector
            cb_url = "https://www.commbuys.com/bso/external/publicBids.sdo?{}".format(
                urllib.parse.urlencode({"keywords": opp.get("title", "")[:60]}))
            links.append('<a class="link-btn" href="{}" target="_blank" rel="noopener">Search COMMBUYS</a>'.format(
                escape_html(cb_url)))
        else:
            # Company opportunity search for private sector
            company_q = urllib.parse.quote('{} careers opportunities Massachusetts'.format(
                opp.get("agency", "")[:40]))
            links.append('<a class="link-btn" href="https://www.google.com/search?q={}" target="_blank" rel="noopener">Search Opportunities</a>'.format(company_q))

        # Google news search
        search_q = urllib.parse.quote('"{}" Massachusetts transportation'.format(
            opp.get("title", "")[:60]))
        links.append('<a class="link-btn" href="https://www.google.com/search?q={}" target="_blank" rel="noopener">Search News</a>'.format(search_q))

        # Mailto link
        if opp.get("contact_email"):
            subject = urllib.parse.quote("Inquiry: {}".format(opp.get("title", "")[:80]))
            links.append('<a class="link-btn" href="mailto:{}?subject={}">Contact</a>'.format(
                escape_html(opp["contact_email"]), subject))

        links_html = '<div class="card-links">{}</div>'.format("".join(links)) if links else ""

        # Search text data attribute
        search_parts = [
            opp.get("title", ""),
            opp.get("agency", ""),
            opp.get("description", ""),
            " ".join(opp.get("keywords_matched", [])),
            opp.get("contact_name", ""),
            opp.get("service_type", ""),
            opp.get("place_of_performance", ""),
            opp.get("naics_code", ""),
            opp.get("notes", ""),
            sector,
            opp_type,
        ]
        search_text = " ".join(search_parts).lower().replace('"', "&quot;")

        # Notes
        notes_html = ""
        if opp.get("notes"):
            notes_html = '<div class="card-notes"><strong>Notes:</strong> {}</div>'.format(
                escape_html(opp["notes"]))

        return (
            '<div class="opp-card{private_class}" '
            'data-source="{data_source}" '
            'data-sector="{data_sector}" '
            'data-opportunity-type="{data_opp_type}" '
            'data-relevance="{data_rel}" '
            'data-service-type="{data_stype}" '
            'data-status="{data_status}" '
            'data-is-new="{data_new}" '
            'data-date="{data_date}" '
            'data-deadline="{data_deadline}" '
            'data-search="{data_search}">'
            '<div class="card-header">'
            '<div class="card-title-row">'
            '<strong class="card-name">{title}</strong>'
            '<div class="card-badges">'
            '<span class="badge" style="background:{sector_color};">{sector_label}</span>'
            '<span class="badge" style="background:{source_color};">{source_label}</span>'
            '<span class="badge" style="background:{rel_color};">{rel_upper}</span>'
            '<span class="badge" style="background:{stype_color};">{stype}</span>'
            '{new_badge}'
            '</div></div>'
            '<div class="card-sub">{agency}'
            '{naics_span}'
            '</div></div>'
            '<div class="card-detail">'
            '<strong>Posted:</strong> {posted} &bull; '
            '<strong>Deadline:</strong> {deadline} &bull; '
            '<strong>Award:</strong> {award}'
            '</div>'
            '<div class="card-detail">'
            '<strong>Location:</strong> {pop}'
            '</div>'
            '<div class="card-desc">{desc}</div>'
            '{contact_html}'
            '{kw_html}'
            '{notes_html}'
            '{links_html}'
            '</div>'
        ).format(
            private_class=" private-card" if sector == "private" else "",
            data_source=escape_html(source),
            data_sector=escape_html(sector),
            data_opp_type=escape_html(opp_type),
            data_rel=escape_html(rel),
            data_stype=escape_html(stype),
            data_status=escape_html(opp.get("status", "active")),
            data_new="true" if opp.get("is_new") else "false",
            data_date=escape_html(posted),
            data_deadline=escape_html(deadline),
            data_search=search_text[:500],
            title=escape_html(opp.get("title", "")[:120]),
            sector_color=sector_color,
            sector_label=sector_label,
            source_color=source_color,
            source_label=escape_html(source),
            rel_color=rel_color,
            rel_upper=rel.upper(),
            stype_color=stype_color,
            stype=escape_html(stype),
            new_badge=new_badge,
            agency=escape_html(opp.get("agency", "N/A")),
            naics_span=' &bull; NAICS: {}'.format(escape_html(opp.get("naics_code", ""))) if opp.get("naics_code") else "",
            posted=escape_html(posted) if posted else "N/A",
            deadline=escape_html(deadline) if deadline else "N/A",
            award=award_display,
            pop=escape_html(opp.get("place_of_performance", "N/A")),
            desc=escape_html(desc_short),
            contact_html=contact_html,
            kw_html=kw_html,
            notes_html=notes_html,
            links_html=links_html,
        )

    cards_html = "".join(render_card(o) for o in all_opportunities)

    # Service type filter options
    stype_options = "".join(
        '<option value="{v}">{v}</option>'.format(v=escape_html(s)) for s in service_types
    )
    status_options = "".join(
        '<option value="{v}">{v}</option>'.format(v=escape_html(s)) for s in statuses
    )

    html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MA Transportation Opportunity Tracker</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: #f5f5f5;
            color: #333;
            line-height: 1.5;
            padding: 12px;
            max-width: 900px;
            margin: 0 auto;
        }}
        h1 {{ font-size: 1.3em; margin-bottom: 4px; }}
        .summary {{
            background: #fff;
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 16px;
            border: 1px solid #ddd;
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
            gap: 8px;
            margin-top: 8px;
        }}
        .stat {{
            text-align: center;
            padding: 8px 4px;
            background: #f9f9f9;
            border-radius: 6px;
        }}
        .stat-num {{ font-size: 1.5em; font-weight: bold; color: #2c3e50; }}
        .stat-label {{ font-size: 0.72em; color: #888; }}
        .commbuys-section, .directory-section {{
            background: #fff;
            border-radius: 8px;
            padding: 0;
            margin-bottom: 16px;
            border: 1px solid #ddd;
        }}
        .commbuys-section summary, .directory-section summary {{
            padding: 12px;
            cursor: pointer;
            font-weight: 600;
            font-size: 0.9em;
            color: #2c3e50;
        }}
        .commbuys-section .cb-body {{
            padding: 0 12px 12px;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .cb-link {{
            border-color: #8e44ad !important;
            color: #8e44ad !important;
        }}
        .cb-link:hover {{
            background: #8e44ad !important;
            color: #fff !important;
        }}
        /* Directory section */
        .directory-section {{ border-color: #e67e22; }}
        .directory-section summary {{ color: #e67e22; }}
        .dir-body {{ padding: 0 12px 12px; }}
        .dir-cat-title {{
            font-size: 0.9em;
            color: #555;
            margin: 12px 0 6px;
            padding-bottom: 4px;
            border-bottom: 1px solid #eee;
        }}
        .dir-cat-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
            gap: 10px;
        }}
        .dir-card {{
            border: 1px solid #eee;
            border-left: 3px solid #e67e22;
            border-radius: 6px;
            padding: 10px;
            background: #fefefe;
        }}
        .dir-card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 4px;
        }}
        .dir-card-header strong {{ font-size: 0.88em; }}
        .dir-status {{ font-size: 0.75em; white-space: nowrap; }}
        .dir-card-desc {{ font-size: 0.8em; color: #555; margin-top: 4px; }}
        .dir-card-earning {{ font-size: 0.8em; color: #27ae60; margin-top: 4px; }}
        .dir-card-links {{ margin-top: 6px; }}
        .dir-link {{
            border-color: #e67e22 !important;
            color: #e67e22 !important;
        }}
        .dir-link:hover {{
            background: #e67e22 !important;
            color: #fff !important;
        }}
        .toolbar {{
            background: #fff;
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 16px;
            border: 1px solid #ddd;
        }}
        .search-input {{
            width: 100%;
            padding: 10px 14px;
            font-size: 1em;
            border: 2px solid #ddd;
            border-radius: 6px;
            outline: none;
            margin-bottom: 10px;
        }}
        .search-input:focus {{
            border-color: #2980b9;
            box-shadow: 0 0 0 3px rgba(41,128,185,0.15);
        }}
        .filter-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
        }}
        .filter-select {{
            padding: 7px 10px;
            font-size: 0.85em;
            border: 1px solid #ddd;
            border-radius: 6px;
            background: #fff;
            outline: none;
            flex: 1 1 120px;
            min-width: 100px;
        }}
        .filter-select:focus {{
            border-color: #2980b9;
        }}
        .csv-btn {{
            display: inline-block;
            padding: 7px 14px;
            font-size: 0.85em;
            background: #27ae60;
            color: #fff;
            border: none;
            border-radius: 6px;
            text-decoration: none;
            font-weight: 600;
            cursor: pointer;
            flex-shrink: 0;
        }}
        .csv-btn:hover {{
            background: #219a52;
        }}
        .filter-count {{
            font-size: 0.85em;
            color: #888;
            margin-top: 8px;
        }}
        .opp-card {{
            border: 1px solid #ddd;
            border-left: 4px solid #2980b9;
            border-radius: 8px;
            padding: 12px;
            margin-bottom: 12px;
            background: #fff;
        }}
        .opp-card.private-card {{
            border-left-color: #e67e22;
        }}
        .opp-card[data-is-new="true"] {{
            border-left-color: #27ae60;
        }}
        .card-header {{
            margin-bottom: 6px;
        }}
        .card-title-row {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .card-name {{
            font-size: 1em;
            flex: 1 1 auto;
            min-width: 0;
            overflow-wrap: break-word;
        }}
        .card-badges {{
            display: flex;
            gap: 4px;
            flex-wrap: wrap;
            flex-shrink: 0;
        }}
        .badge {{
            color: #fff;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.7em;
            font-weight: bold;
            white-space: nowrap;
        }}
        .badge-new {{
            background: #27ae60;
        }}
        .card-sub {{
            color: #666;
            font-size: 0.85em;
            margin-top: 4px;
        }}
        .card-detail {{
            font-size: 0.85em;
            margin-top: 4px;
        }}
        .card-desc {{
            font-size: 0.85em;
            margin-top: 6px;
            color: #444;
        }}
        .card-contact {{
            font-size: 0.82em;
            margin-top: 6px;
            color: #555;
        }}
        .card-contact a {{
            color: #2980b9;
        }}
        .card-keywords {{
            margin-top: 6px;
            font-size: 0.85em;
        }}
        .kw-tag {{
            background: #eee;
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.85em;
            margin: 2px;
            display: inline-block;
        }}
        .card-notes {{
            font-size: 0.82em;
            margin-top: 6px;
            color: #666;
            font-style: italic;
        }}
        .card-links {{
            margin-top: 8px;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }}
        .link-btn {{
            display: inline-block;
            padding: 4px 10px;
            font-size: 0.78em;
            border: 1px solid #2980b9;
            border-radius: 4px;
            color: #2980b9;
            text-decoration: none;
            font-weight: 600;
        }}
        .link-btn:hover {{
            background: #2980b9;
            color: #fff;
        }}
        .no-results {{
            text-align: center;
            color: #888;
            padding: 32px 12px;
            font-size: 1em;
            display: none;
        }}
        @media (max-width: 600px) {{
            .filter-row {{
                flex-direction: column;
            }}
            .filter-select, .csv-btn {{
                flex: 1 1 100%;
                width: 100%;
            }}
            .card-title-row {{
                flex-direction: column;
            }}
            .dir-cat-grid {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
        <div>
            <h1>MA Transportation Opportunity Tracker</h1>
            <p style="color:#888;font-size:0.85em;">
                NEMT, courier, paratransit, shuttle, freight, rideshare &amp; transport opportunities in Massachusetts &bull;
                Public &amp; Private Sector &bull;
                Updated {run_time} UTC
            </p>
        </div>
        <button id="refreshBtn" onclick="triggerRefresh()" style="padding:8px 16px;font-size:0.85em;font-weight:600;background:#2980b9;color:#fff;border:none;border-radius:6px;cursor:pointer;white-space:nowrap;height:fit-content;">Refresh Report</button>
    </div>
    <div id="refreshStatus" style="display:none;margin-top:6px;margin-bottom:8px;padding:8px 12px;border-radius:6px;font-size:0.85em;"></div>

    <div class="summary">
        <div class="summary-grid">
            <div class="stat">
                <div class="stat-num">{summary_total}</div>
                <div class="stat-label">Total Tracked</div>
            </div>
            <div class="stat">
                <div class="stat-num">{summary_new}</div>
                <div class="stat-label">New This Run</div>
            </div>
            <div class="stat">
                <div class="stat-num">{public_count}</div>
                <div class="stat-label">Public Sector</div>
            </div>
            <div class="stat">
                <div class="stat-num">{private_count}</div>
                <div class="stat-label">Private Sector</div>
            </div>
            <div class="stat">
                <div class="stat-num">{sam_count}</div>
                <div class="stat-label">SAM.gov</div>
            </div>
            <div class="stat">
                <div class="stat-num">{feed_count}</div>
                <div class="stat-label">Job Feeds</div>
            </div>
            <div class="stat">
                <div class="stat-num">{dir_count}</div>
                <div class="stat-label">Directory</div>
            </div>
            <div class="stat">
                <div class="stat-num">{high_count}</div>
                <div class="stat-label">High Relevance</div>
            </div>
        </div>
    </div>

    <details class="commbuys-section">
        <summary>COMMBUYS Quick Links &mdash; Search MA procurement portal</summary>
        <div class="cb-body">
            {cb_links_html}
        </div>
    </details>

    {dir_section_html}

    <div class="toolbar">
        <input type="text" id="searchInput" class="search-input"
               placeholder="Search title, agency, description, keywords, sector, type...">
        <div class="filter-row">
            <select id="filterSector" class="filter-select">
                <option value="">All Sectors</option>
                <option value="public">Public</option>
                <option value="private">Private</option>
            </select>
            <select id="filterSource" class="filter-select">
                <option value="">All Sources</option>
                <option value="SAM.gov">SAM.gov</option>
                <option value="Manual">Manual</option>
                <option value="Craigslist">Craigslist</option>
                <option value="Indeed">Indeed</option>
                <option value="Directory">Directory</option>
            </select>
            <select id="filterRelevance" class="filter-select">
                <option value="">All Relevance</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
            </select>
            <select id="filterServiceType" class="filter-select">
                <option value="">All Service Types</option>
                {stype_options}
            </select>
            <select id="filterStatus" class="filter-select">
                <option value="">All Statuses</option>
                {status_options}
            </select>
            <select id="sortOrder" class="filter-select">
                <option value="default">Sort: Default</option>
                <option value="newest">Sort: Newest First</option>
                <option value="oldest">Sort: Oldest First</option>
                <option value="deadline">Sort: Deadline Soonest</option>
            </select>
            <a href="opportunities.csv" class="csv-btn" download>Download CSV</a>
        </div>
        <div class="filter-count" id="filterCount"></div>
    </div>

    <div id="cardContainer">
        {cards_html}
    </div>
    <div class="no-results" id="noResults">No opportunities match your filters.</div>

    <footer style="margin-top:24px;padding-top:12px;border-top:1px solid #ddd;color:#aaa;font-size:0.75em;text-align:center;">
        Data from <a href="https://sam.gov" style="color:#aaa;">SAM.gov</a>,
        <a href="https://www.commbuys.com" style="color:#aaa;">COMMBUYS</a>,
        <a href="https://boston.craigslist.org" style="color:#aaa;">Craigslist</a>,
        <a href="https://www.indeed.com" style="color:#aaa;">Indeed</a>
        &amp; curated private sector directory &bull;
        MA Transportation Opportunity Tracker
    </footer>

    <script>
    (function() {{
        var cards = [];
        var container = document.getElementById('cardContainer');
        var noResults = document.getElementById('noResults');
        var countEl = document.getElementById('filterCount');
        var searchInput = document.getElementById('searchInput');
        var filterSector = document.getElementById('filterSector');
        var filterSource = document.getElementById('filterSource');
        var filterRelevance = document.getElementById('filterRelevance');
        var filterServiceType = document.getElementById('filterServiceType');
        var filterStatus = document.getElementById('filterStatus');
        var sortOrder = document.getElementById('sortOrder');
        var debounceTimer = null;

        var els = container.getElementsByClassName('opp-card');
        for (var i = 0; i < els.length; i++) {{
            cards.push(els[i]);
        }}
        var total = cards.length;

        function applyFilters() {{
            var q = searchInput.value.toLowerCase().trim();
            var sec = filterSector.value;
            var src = filterSource.value;
            var rel = filterRelevance.value;
            var stype = filterServiceType.value;
            var stat = filterStatus.value;
            var shown = 0;

            for (var i = 0; i < cards.length; i++) {{
                var c = cards[i];
                var visible = true;
                if (sec && c.getAttribute('data-sector') !== sec) visible = false;
                if (src && c.getAttribute('data-source') !== src) visible = false;
                if (rel && c.getAttribute('data-relevance') !== rel) visible = false;
                if (stype && c.getAttribute('data-service-type') !== stype) visible = false;
                if (stat && c.getAttribute('data-status') !== stat) visible = false;
                if (q && c.getAttribute('data-search').indexOf(q) === -1) visible = false;
                c.style.display = visible ? '' : 'none';
                if (visible) shown++;
            }}

            countEl.textContent = 'Showing ' + shown + ' of ' + total + ' opportunities';
            noResults.style.display = (shown === 0) ? 'block' : 'none';
        }}

        function applySort() {{
            var order = sortOrder.value;
            if (order === 'default') return;

            var sorted = cards.slice().sort(function(a, b) {{
                if (order === 'newest' || order === 'oldest') {{
                    var da = a.getAttribute('data-date') || '';
                    var db = b.getAttribute('data-date') || '';
                    if (order === 'newest') return da < db ? 1 : (da > db ? -1 : 0);
                    return da > db ? 1 : (da < db ? -1 : 0);
                }}
                if (order === 'deadline') {{
                    var dla = a.getAttribute('data-deadline') || 'zzzz';
                    var dlb = b.getAttribute('data-deadline') || 'zzzz';
                    return dla > dlb ? 1 : (dla < dlb ? -1 : 0);
                }}
                return 0;
            }});

            for (var i = 0; i < sorted.length; i++) {{
                container.appendChild(sorted[i]);
            }}
        }}

        function update() {{
            applySort();
            applyFilters();
        }}

        searchInput.addEventListener('input', function() {{
            if (debounceTimer) clearTimeout(debounceTimer);
            debounceTimer = setTimeout(update, 200);
        }});

        filterSector.addEventListener('change', update);
        filterSource.addEventListener('change', update);
        filterRelevance.addEventListener('change', update);
        filterServiceType.addEventListener('change', update);
        filterStatus.addEventListener('change', update);
        sortOrder.addEventListener('change', update);

        applyFilters();
    }})();

    function triggerRefresh() {{
        var btn = document.getElementById('refreshBtn');
        var status = document.getElementById('refreshStatus');
        var token = localStorage.getItem('gh_pat');
        if (!token) {{
            token = prompt('Enter your GitHub Personal Access Token (needs workflow scope).\\nThis will be saved in your browser for future use.');
            if (!token) return;
            localStorage.setItem('gh_pat', token);
        }}
        btn.disabled = true;
        btn.textContent = 'Triggering...';
        btn.style.background = '#7f8c8d';
        status.style.display = 'block';
        status.style.background = '#eaf4fd';
        status.style.color = '#2980b9';
        status.textContent = 'Triggering workflow... The report will update in a few minutes.';

        fetch('https://api.github.com/repos/kwamAG/ma_transport_tracker/actions/workflows/weekly.yml/dispatches', {{
            method: 'POST',
            headers: {{
                'Authorization': 'Bearer ' + token,
                'Accept': 'application/vnd.github.v3+json',
                'Content-Type': 'application/json'
            }},
            body: JSON.stringify({{ ref: 'main' }})
        }}).then(function(r) {{
            if (r.status === 204) {{
                status.style.background = '#eafaf1';
                status.style.color = '#27ae60';
                status.textContent = 'Workflow triggered! The report will refresh automatically in a few minutes. Reload this page after that to see updated data.';
            }} else if (r.status === 401 || r.status === 403) {{
                localStorage.removeItem('gh_pat');
                status.style.background = '#fdecea';
                status.style.color = '#c0392b';
                status.textContent = 'Invalid or expired token. Click Refresh again to enter a new one.';
            }} else {{
                status.style.background = '#fdecea';
                status.style.color = '#c0392b';
                status.textContent = 'Error: HTTP ' + r.status + '. Check your token permissions.';
            }}
            btn.disabled = false;
            btn.textContent = 'Refresh Report';
            btn.style.background = '#2980b9';
        }}).catch(function(e) {{
            status.style.background = '#fdecea';
            status.style.color = '#c0392b';
            status.textContent = 'Network error: ' + e.message;
            btn.disabled = false;
            btn.textContent = 'Refresh Report';
            btn.style.background = '#2980b9';
        }});
    }}
    </script>
</body>
</html>""".format(
        run_time=run_time.strftime('%B %d, %Y at %I:%M %p'),
        summary_total=summary_total,
        summary_new=summary_new,
        public_count=public_count,
        private_count=private_count,
        sam_count=sam_count,
        feed_count=feed_count,
        dir_count=dir_count,
        high_count=high_count,
        cb_links_html=cb_links_html,
        dir_section_html=dir_section_html,
        stype_options=stype_options,
        status_options=status_options,
        cards_html=cards_html,
    )
    return html


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def main():
    print("MA Transportation Opportunity Tracker")
    print("=" * 50)

    # 1. Load config + seen opportunities
    config = load_json(CONFIG_PATH)
    seen = load_json(SEEN_PATH)
    seen_sam_ids = set(seen.get("sam_gov", []))
    seen_manual_ids = set(seen.get("manual", []))
    seen_craigslist_ids = set(seen.get("craigslist", []))
    seen_indeed_ids = set(seen.get("indeed", []))
    seen_directory_ids = set(seen.get("directory", []))
    print("Config loaded. States: {} | NAICS codes: {}".format(
        ", ".join(config.get("states", [])),
        ", ".join(config.get("naics_codes", []))))

    # 2. Fetch SAM.gov opportunities
    print("\n[1/5] Fetching SAM.gov opportunities...")
    raw_sam = fetch_all_sam_opportunities(config)
    print("  Total SAM.gov raw results: {}".format(len(raw_sam)))

    # 3. Load manual entries
    print("\n[2/5] Loading manual opportunities...")
    try:
        manual_entries = load_json(MANUAL_PATH)
        print("  Loaded {} manual entries".format(len(manual_entries)))
    except Exception as e:
        print("  Could not load manual entries: {}".format(e))
        manual_entries = []

    # 4. Fetch Craigslist RSS feeds
    print("\n[3/5] Fetching Craigslist RSS feeds...")
    craigslist_opps = fetch_craigslist_opportunities(config)
    print("  Total Craigslist results: {}".format(len(craigslist_opps)))

    # 5. Fetch Indeed RSS feeds
    print("\n[4/5] Fetching Indeed RSS feeds...")
    indeed_opps = fetch_indeed_opportunities(config)
    print("  Total Indeed results: {}".format(len(indeed_opps)))

    # 6. Check private directory entries
    print("\n[5/5] Checking private directory entries...")
    directory_opps = check_directory_entries(config)
    print("  Total directory entries: {}".format(len(directory_opps)))

    # 7. Process SAM.gov and manual sources
    print("\nProcessing opportunities...")
    sam_opps = process_sam_opportunities(raw_sam, config)
    print("  {} SAM.gov opportunities after filtering".format(len(sam_opps)))

    manual_opps = process_manual_opportunities(manual_entries, config)
    print("  {} manual opportunities after filtering".format(len(manual_opps)))

    # 8. Mark new/seen across all sources
    sam_opps = identify_new_opportunities(sam_opps, seen_sam_ids)
    manual_opps = identify_new_opportunities(manual_opps, seen_manual_ids)
    craigslist_opps = identify_new_opportunities(craigslist_opps, seen_craigslist_ids)
    indeed_opps = identify_new_opportunities(indeed_opps, seen_indeed_ids)
    directory_opps = identify_new_opportunities(directory_opps, seen_directory_ids)

    # 9. Combine all sources
    all_opps = sam_opps + manual_opps + craigslist_opps + indeed_opps + directory_opps

    new_sam = sum(1 for o in sam_opps if o.get("is_new"))
    new_manual = sum(1 for o in manual_opps if o.get("is_new"))
    new_cl = sum(1 for o in craigslist_opps if o.get("is_new"))
    new_indeed = sum(1 for o in indeed_opps if o.get("is_new"))
    new_dir = sum(1 for o in directory_opps if o.get("is_new"))

    print("\n  New SAM.gov: {}".format(new_sam))
    print("  New manual: {}".format(new_manual))
    print("  New Craigslist: {}".format(new_cl))
    print("  New Indeed: {}".format(new_indeed))
    print("  New directory: {}".format(new_dir))

    # 10. Generate COMMBUYS links
    commbuys_links = get_commbuys_search_links(config)

    # 11. Generate HTML
    run_time = datetime.now(timezone.utc)
    print("\nGenerating HTML report...")
    html = generate_html(all_opps, commbuys_links, directory_opps, run_time)

    os.makedirs(os.path.dirname(OUTPUT_HTML), exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print("  Report saved to {}".format(OUTPUT_HTML))

    # 12. Generate CSV
    print("Generating CSV export...")
    generate_csv(all_opps, OUTPUT_CSV)
    print("  CSV saved to {}".format(OUTPUT_CSV))

    # 13. Update seen_opportunities.json
    seen["sam_gov"] = list(seen_sam_ids | {o["id"] for o in sam_opps})
    seen["manual"] = list(seen_manual_ids | {o["id"] for o in manual_opps})
    seen["craigslist"] = list(seen_craigslist_ids | {o["id"] for o in craigslist_opps})
    seen["indeed"] = list(seen_indeed_ids | {o["id"] for o in indeed_opps})
    seen["directory"] = list(seen_directory_ids | {o["id"] for o in directory_opps})
    seen["last_run"] = run_time.isoformat()
    save_json(SEEN_PATH, seen)
    print("  Updated seen_opportunities.json")

    # 14. Print summary
    public_count = sum(1 for o in all_opps if o.get("sector") == "public")
    private_count = sum(1 for o in all_opps if o.get("sector") == "private")

    print("\n" + "=" * 50)
    print("Summary:")
    print("  Total opportunities: {}".format(len(all_opps)))
    print("  Public sector: {} | Private sector: {}".format(public_count, private_count))
    print("  SAM.gov: {} ({} new)".format(len(sam_opps), new_sam))
    print("  Manual: {} ({} new)".format(len(manual_opps), new_manual))
    print("  Craigslist: {} ({} new)".format(len(craigslist_opps), new_cl))
    print("  Indeed: {} ({} new)".format(len(indeed_opps), new_indeed))
    print("  Directory: {} ({} new)".format(len(directory_opps), new_dir))
    high = sum(1 for o in all_opps if o.get("relevance") == "high")
    med = sum(1 for o in all_opps if o.get("relevance") == "medium")
    low = sum(1 for o in all_opps if o.get("relevance") == "low")
    print("  High relevance: {} | Medium: {} | Low: {}".format(high, med, low))
    print("\nDone! Open {} in a browser to view the report.".format(OUTPUT_HTML))


if __name__ == "__main__":
    main()
