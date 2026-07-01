# Hedge Fund Contact Enrichment

Enriches 912 active hedge fund manager names with email, phone, and website.

## Setup

```bash
pip install -r requirements.txt
```

## Files

| File | Purpose |
|------|---------|
| `active_managers.csv` | Input: 912 active managers from NilssonHedge DB |
| `enrich.py` | Main enrichment pipeline |
| `enriched_contacts.csv` | Output: enriched contacts (created on run) |
| `progress.json` | Auto-saved progress (for resume) |
| `enrich.log` | Full run log |

## Usage

### Test run (first 10 managers)
```bash
python enrich.py --limit 10
```

### Full run (all 912)
```bash
python enrich.py
```

### Resume after interruption
```bash
python enrich.py --resume
```

### With Hunter.io API key (improves email hit rate)
```bash
python enrich.py --hunter-key YOUR_HUNTER_KEY
```
Get a free key at https://hunter.io (25 searches/month free, 500/month on $49 plan)

## Sources Used

| Source | Fund Types | What it provides |
|--------|-----------|-----------------|
| SEC IAPD | Hedge Funds, Asset Managers | CRD number, firm info |
| NFA BASIC | CTAs (45% of list) | Registration status, NFA ID |
| DuckDuckGo | All | Website discovery |
| Website scrape | All | Email, phone from contact/IR pages |
| Hunter.io (optional) | All | Verified business emails |

## Output Columns

| Column | Description |
|--------|-------------|
| manager_id | NilssonHedge Manager ID |
| manager_name | Fund manager name |
| type / style / strategy / sector | Fund classification |
| website | Official website |
| email_1 / email_2 | Best emails found |
| phone_1 / phone_2 | Phone numbers found |
| email_score | Confidence score 0–100 |
| contact_name / contact_title | Person name if found (Hunter only) |
| nfa_id / nfa_status | NFA registration details |
| sec_crd | SEC CRD number |
| source | Which sources provided data |
| status | Verified / Needs Review / No Reliable Contact Found |
| last_checked | Date of enrichment run |

## Status Meanings

- **Verified** — email found with score ≥ 60 (domain matches website, or strong source)
- **Needs Review** — some data found but confidence is lower
- **No Reliable Contact Found** — public sources had nothing; may need paid data

## Expected Hit Rates (without Hunter)
- ~40–55% Verified or Needs Review
- ~45–60% No Contact Found (especially non-US, crypto, and small closed funds)

## Runtime
- ~912 managers × ~4 seconds per manager = ~60–90 minutes for full run
- Runs politely (1.2s delay between requests)
- Safe to interrupt (Ctrl+C) and resume with `--resume`
