#!/usr/bin/env python3
"""MA Transportation Opportunity Tracker.

Finds NEMT, courier, paratransit, shuttle, and diversified transport contract
opportunities in Massachusetts via SAM.gov and manual entries. Generates a
mobile-friendly HTML report and CSV export. Uses only Python standard library.
"""

import json
import os
import csv
import ssl
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


# ---------------------------------------------------------------------------
# SAM.gov API integration
# ---------------------------------------------------------------------------

def api_fetch_sam(config, naics_code, offset=0, limit=25):
    """Query SAM.gov opportunities API for a single NAICS code + state.

    Returns parsed JSON response or None on error.
    """
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

    # Add state filter
    for state in config.get("states", ["MA"]):
        params += "&state=" + urllib.parse.quote(state)

    url = "{}?{}".format(base_url, params)
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "MATransportTracker/1.0"})

    try:
        with urllib.request.urlopen(req, context=ctx, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data
    except Exception as e:
        print("  SAM.gov API error (NAICS {}): {}".format(naics_code, e))
        return None


def fetch_all_sam_opportunities(config):
    """Fetch opportunities from SAM.gov across all configured NAICS codes.

    Paginates through results and deduplicates by noticeId.
    Returns list of raw opportunity dicts.
    """
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
        max_pages = 40  # Safety limit

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
# Processing and scoring
# ---------------------------------------------------------------------------

def score_relevance(matched_direct, matched_service, award_amount, auto_high_value):
    """Score relevance: 'high', 'medium', or 'low'.

    High: direct keyword match OR award >= auto_high_value
    Medium: service type keyword match
    Low: NAICS match only
    """
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
    Logistics, Other Transport
    """
    if not text:
        text = ""
    lower = text.lower()
    kw_str = " ".join(matched_keywords).lower() if matched_keywords else ""
    combined = lower + " " + kw_str

    nemt_terms = ["nemt", "non-emergency medical", "medical transport",
                  "patient transport", "medicaid transport", "medical transportation"]
    if any(t in combined for t in nemt_terms):
        return "NEMT"

    para_terms = ["paratransit", "dial-a-ride", "wheelchair", "stretcher",
                  "ambulatory", "ada transport"]
    if any(t in combined for t in para_terms):
        return "Paratransit"

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
    """Process raw SAM.gov results: filter exclusions, match keywords, score.

    Returns list of standardized opportunity dicts.
    """
    direct_kw = config.get("direct_transport_keywords", [])
    service_kw = config.get("service_type_keywords", [])
    exclude_kw = config.get("exclude_keywords", [])
    auto_high = config.get("auto_high_value", 500000)
    processed = []

    for opp in raw_opps:
        title = opp.get("title", "") or ""
        description = opp.get("description", "") or ""
        # Build searchable text
        search_text = " ".join([
            title,
            description,
            opp.get("organizationName", "") or "",
            opp.get("placeOfPerformance", {}).get("state", {}).get("name", "") if isinstance(opp.get("placeOfPerformance"), dict) else "",
        ])

        # Check exclusions
        if contains_excluded(search_text, exclude_kw):
            continue

        matched_direct = match_keywords(search_text, direct_kw)
        matched_service = match_keywords(search_text, service_kw)

        # Award amount
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

        # Place of performance
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

        # Contact info
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
            "status": "active",
            "is_new": False,
            "notes": "",
        })

    return processed


def process_manual_opportunities(entries, config):
    """Process manual opportunity entries through the same pipeline.

    Returns list of standardized opportunity dicts.
    """
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
    "keywords_matched", "relevance", "service_type", "source", "status",
    "is_new", "notes",
]


def generate_csv(all_opportunities, output_path):
    """Write all opportunities to a CSV file with BOM for Excel compatibility."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for opp in all_opportunities:
            row = dict(opp)
            # Flatten list fields
            if isinstance(row.get("keywords_matched"), list):
                row["keywords_matched"] = "; ".join(row["keywords_matched"])
            writer.writerow(row)


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def generate_html(all_opportunities, commbuys_links, run_time):
    """Generate mobile-friendly HTML report with search, filters, and sorting."""

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
    sam_count = sum(1 for o in all_opportunities if o.get("source") == "SAM.gov")
    manual_count = sum(1 for o in all_opportunities if o.get("source") == "Manual")
    high_count = sum(1 for o in all_opportunities if o.get("relevance") == "high")
    active_count = sum(1 for o in all_opportunities if o.get("status", "").lower() == "active")

    # Collect service types and statuses for filter dropdowns
    service_types = sorted(set(o.get("service_type", "Other Transport") for o in all_opportunities))
    statuses = sorted(set(o.get("status", "active") for o in all_opportunities))

    # COMMBUYS links HTML
    cb_links_html = ""
    for label, url in commbuys_links:
        cb_links_html += '<a class="link-btn cb-link" href="{}" target="_blank" rel="noopener">{}</a>\n'.format(
            escape_html(url), escape_html(label))

    # --- Render cards ---
    def render_card(opp):
        source = opp.get("source", "SAM.gov")
        is_sam = source == "SAM.gov"
        source_color = "#2980b9" if is_sam else "#8e44ad"
        source_label = source

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

        # Links row
        links = []
        if opp.get("url"):
            link_label = "View on SAM.gov" if is_sam else "View Source"
            links.append('<a class="link-btn" href="{}" target="_blank" rel="noopener">{}</a>'.format(
                escape_html(opp["url"]), link_label))

        # COMMBUYS search
        title_q = urllib.parse.quote(opp.get("title", "")[:60])
        cb_url = "https://www.commbuys.com/bso/external/publicBids.sdo?{}".format(
            urllib.parse.urlencode({"keywords": opp.get("title", "")[:60]}))
        links.append('<a class="link-btn" href="{}" target="_blank" rel="noopener">Search COMMBUYS</a>'.format(
            escape_html(cb_url)))

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
        ]
        search_text = " ".join(search_parts).lower().replace('"', "&quot;")

        # Notes
        notes_html = ""
        if opp.get("notes"):
            notes_html = '<div class="card-notes"><strong>Notes:</strong> {}</div>'.format(
                escape_html(opp["notes"]))

        return (
            '<div class="opp-card" '
            'data-source="{data_source}" '
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
            data_source=escape_html(source),
            data_rel=escape_html(rel),
            data_stype=escape_html(stype),
            data_status=escape_html(opp.get("status", "active")),
            data_new="true" if opp.get("is_new") else "false",
            data_date=escape_html(posted),
            data_deadline=escape_html(deadline),
            data_search=search_text[:500],
            title=escape_html(opp.get("title", "")[:120]),
            source_color=source_color,
            source_label=escape_html(source_label),
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
        .commbuys-section {{
            background: #fff;
            border-radius: 8px;
            padding: 0;
            margin-bottom: 16px;
            border: 1px solid #ddd;
        }}
        .commbuys-section summary {{
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
            flex: 1 1 130px;
            min-width: 110px;
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
        .opp-card[data-is-new="true"] {{
            border-left-color: #27ae60;
        }}
        .opp-card[data-source="Manual"] {{
            border-left-color: #8e44ad;
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
        }}
    </style>
</head>
<body>
    <h1>MA Transportation Opportunity Tracker</h1>
    <p style="color:#888;font-size:0.85em;margin-bottom:12px;">
        NEMT, courier, paratransit, shuttle &amp; transport contracts in Massachusetts &bull;
        Updated {run_time} UTC
    </p>

    <div class="summary">
        <div class="summary-grid">
            <div class="stat">
                <div class="stat-num">{summary_new}</div>
                <div class="stat-label">New This Week</div>
            </div>
            <div class="stat">
                <div class="stat-num">{summary_total}</div>
                <div class="stat-label">Total Tracked</div>
            </div>
            <div class="stat">
                <div class="stat-num">{sam_count}</div>
                <div class="stat-label">SAM.gov</div>
            </div>
            <div class="stat">
                <div class="stat-num">{manual_count}</div>
                <div class="stat-label">Manual</div>
            </div>
            <div class="stat">
                <div class="stat-num">{high_count}</div>
                <div class="stat-label">High Relevance</div>
            </div>
            <div class="stat">
                <div class="stat-num">{active_count}</div>
                <div class="stat-label">Active</div>
            </div>
        </div>
    </div>

    <details class="commbuys-section">
        <summary>COMMBUYS Quick Links &mdash; Search MA procurement portal</summary>
        <div class="cb-body">
            {cb_links_html}
        </div>
    </details>

    <div class="toolbar">
        <input type="text" id="searchInput" class="search-input"
               placeholder="Search title, agency, description, keywords, contact, service type...">
        <div class="filter-row">
            <select id="filterSource" class="filter-select">
                <option value="">All Sources</option>
                <option value="SAM.gov">SAM.gov</option>
                <option value="Manual">Manual</option>
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
        Data from <a href="https://sam.gov" style="color:#aaa;">SAM.gov</a> &amp;
        <a href="https://www.commbuys.com" style="color:#aaa;">COMMBUYS</a> &bull;
        MA Transportation Opportunity Tracker
    </footer>

    <script>
    (function() {{
        var cards = [];
        var container = document.getElementById('cardContainer');
        var noResults = document.getElementById('noResults');
        var countEl = document.getElementById('filterCount');
        var searchInput = document.getElementById('searchInput');
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
            var src = filterSource.value;
            var rel = filterRelevance.value;
            var stype = filterServiceType.value;
            var stat = filterStatus.value;
            var shown = 0;

            for (var i = 0; i < cards.length; i++) {{
                var c = cards[i];
                var visible = true;
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

        filterSource.addEventListener('change', update);
        filterRelevance.addEventListener('change', update);
        filterServiceType.addEventListener('change', update);
        filterStatus.addEventListener('change', update);
        sortOrder.addEventListener('change', update);

        applyFilters();
    }})();
    </script>
</body>
</html>""".format(
        run_time=run_time.strftime('%B %d, %Y at %I:%M %p'),
        summary_new=summary_new,
        summary_total=summary_total,
        sam_count=sam_count,
        manual_count=manual_count,
        high_count=high_count,
        active_count=active_count,
        cb_links_html=cb_links_html,
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
    print("=" * 40)

    # 1. Load config + seen opportunities
    config = load_json(CONFIG_PATH)
    seen = load_json(SEEN_PATH)
    seen_sam_ids = set(seen.get("sam_gov", []))
    seen_manual_ids = set(seen.get("manual", []))
    print("Config loaded. NAICS codes: {}".format(", ".join(config.get("naics_codes", []))))

    # 2. Fetch SAM.gov opportunities (skip if no API key)
    print("\nFetching SAM.gov opportunities...")
    raw_sam = fetch_all_sam_opportunities(config)
    print("  Total SAM.gov raw results: {}".format(len(raw_sam)))

    # 3. Load manual entries
    print("\nLoading manual opportunities...")
    try:
        manual_entries = load_json(MANUAL_PATH)
        print("  Loaded {} manual entries".format(len(manual_entries)))
    except Exception as e:
        print("  Could not load manual entries: {}".format(e))
        manual_entries = []

    # 4. Process both sources
    print("\nProcessing SAM.gov opportunities...")
    sam_opps = process_sam_opportunities(raw_sam, config)
    print("  {} SAM.gov opportunities after filtering".format(len(sam_opps)))

    print("Processing manual opportunities...")
    manual_opps = process_manual_opportunities(manual_entries, config)
    print("  {} manual opportunities after filtering".format(len(manual_opps)))

    # 5. Combine + mark new/seen
    all_opps = sam_opps + manual_opps
    sam_opps = identify_new_opportunities(sam_opps, seen_sam_ids)
    manual_opps = identify_new_opportunities(manual_opps, seen_manual_ids)
    all_opps = sam_opps + manual_opps

    new_sam = sum(1 for o in sam_opps if o.get("is_new"))
    new_manual = sum(1 for o in manual_opps if o.get("is_new"))
    print("\n  New SAM.gov opportunities: {}".format(new_sam))
    print("  New manual opportunities: {}".format(new_manual))

    # 6. Generate COMMBUYS links
    commbuys_links = get_commbuys_search_links(config)

    # 7. Generate HTML
    run_time = datetime.now(timezone.utc)
    print("\nGenerating HTML report...")
    html = generate_html(all_opps, commbuys_links, run_time)

    os.makedirs(os.path.dirname(OUTPUT_HTML), exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print("  Report saved to {}".format(OUTPUT_HTML))

    # 8. Generate CSV
    print("Generating CSV export...")
    generate_csv(all_opps, OUTPUT_CSV)
    print("  CSV saved to {}".format(OUTPUT_CSV))

    # 9. Update seen_opportunities.json
    all_sam_ids = list(seen_sam_ids | {o["id"] for o in sam_opps})
    all_manual_ids = list(seen_manual_ids | {o["id"] for o in manual_opps})
    seen["sam_gov"] = all_sam_ids
    seen["manual"] = all_manual_ids
    seen["last_run"] = run_time.isoformat()
    save_json(SEEN_PATH, seen)
    print("  Updated seen_opportunities.json")

    # 10. Print summary
    print("\n" + "=" * 40)
    print("Summary:")
    print("  Total opportunities: {}".format(len(all_opps)))
    print("  SAM.gov: {} ({} new)".format(len(sam_opps), new_sam))
    print("  Manual: {} ({} new)".format(len(manual_opps), new_manual))
    high = sum(1 for o in all_opps if o.get("relevance") == "high")
    med = sum(1 for o in all_opps if o.get("relevance") == "medium")
    low = sum(1 for o in all_opps if o.get("relevance") == "low")
    print("  High relevance: {} | Medium: {} | Low: {}".format(high, med, low))
    print("\nDone! Open {} in a browser to view the report.".format(OUTPUT_HTML))


if __name__ == "__main__":
    main()
