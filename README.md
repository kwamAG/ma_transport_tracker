# MA Transportation Opportunity Tracker

Finds NEMT, courier, paratransit, shuttle, and diversified transport contract opportunities in Massachusetts. Queries SAM.gov weekly and combines with manually curated leads. Generates a mobile-friendly HTML report and CSV export published via GitHub Pages.

## Data Sources

- **SAM.gov** -- Federal contract opportunities filtered by transport NAICS codes and Massachusetts
- **Manual entries** -- User-curated leads from COMMBUYS, MBTA, MassHealth, and other sources
- **COMMBUYS quick links** -- Direct search links to the MA state procurement portal

## NAICS Codes Tracked

| Code | Description |
|------|-------------|
| 485320 | Limousine Service |
| 485999 | All Other Transit and Ground Passenger Transportation |
| 485310 | Taxi and Ridesharing Services |
| 492110 | Couriers and Express Delivery Services |
| 621910 | Ambulance Services (includes NEMT) |
| 488999 | All Other Support Activities for Transportation |

## Setup

1. Create a GitHub repo named `ma_transport_tracker`
2. Push this code to the repo
3. **Register for a SAM.gov API key** at https://sam.gov/content/entity-registration -- go to https://open.gsa.gov/api/get-opportunities-public-api/ and request a key
4. Add the key as a GitHub secret: Settings > Secrets and variables > Actions > New repository secret > Name: `SAM_API_KEY`
5. Enable GitHub Pages: Settings > Pages > Source: deploy from branch `main`, folder `/docs`
6. The workflow runs automatically every Monday at 7 AM EST
7. View the report at `https://<your-username>.github.io/ma_transport_tracker/`

## Run Locally

```bash
python3 tracker.py
open docs/index.html
```

No pip installs needed -- uses only Python standard library.

The tracker works without a SAM.gov API key (manual entries only). To include SAM.gov results locally, add your key to `config.json`:

```json
{
  "sam_api_key": "YOUR_KEY_HERE"
}
```

## Adding Manual Opportunities

Edit `manual_opportunities.json` to add leads you find on COMMBUYS, MBTA procurement, MassHealth RFPs, or other sources. Each entry needs:

```json
{
  "id": "manual-003",
  "title": "Description of the opportunity",
  "agency": "Issuing agency name",
  "source_detail": "Where you found it",
  "posted_date": "2025-01-15",
  "response_deadline": "2025-03-01",
  "award_amount": 250000,
  "naics_code": "485320",
  "place_of_performance": "Boston, MA",
  "description": "Full description of the opportunity...",
  "contact_name": "Contact Name",
  "contact_email": "contact@example.com",
  "contact_phone": "617-555-0100",
  "url": "https://link-to-source",
  "status": "active",
  "notes": "Any additional notes"
}
```

Use `manual-NNN` format for IDs to avoid conflicts with SAM.gov notice IDs.

## Configuration

Edit `config.json` to customize:

- `sam_api_key` -- Your SAM.gov API key (leave empty to skip SAM.gov)
- `search_days_back` -- How far back to search SAM.gov (default: 365 days)
- `naics_codes` -- NAICS codes to search
- `states` -- State filter (default: MA)
- `direct_transport_keywords` -- High-relevance keywords (NEMT, paratransit, etc.)
- `service_type_keywords` -- Medium-relevance keywords (courier, shuttle, etc.)
- `exclude_keywords` -- Terms to filter out (school bus, etc.)
- `auto_high_value` -- Award threshold for automatic high relevance (default: $500,000)
