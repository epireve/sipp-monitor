# sipp-monitor

Automated daily connectivity monitoring for all **349 Indonesian district court SIPP (Sistem Informasi Penelusuran Perkara)** URLs, with state-diffing, markdown reports, and GitHub issue alerts for major jurisdiction outages.

[![SIPP Daily Check](https://github.com/epireve/sipp-monitor/actions/workflows/sipp-daily-check.yml/badge.svg)](https://github.com/epireve/sipp-monitor/actions/workflows/sipp-daily-check.yml)

---

## What it does

| Feature | Detail |
|---------|--------|
| **Daily check** | Runs 09:30 MYT (01:30 UTC), Monday–Friday |
| **349 courts** | All Pengadilan Negeri across 34 provinces |
| **State diffing** | Detects newly-down and recovered courts each run |
| **Markdown reports** | Saved to `reports/YYYY-MM-DD.md`, latest at `reports/latest.md` |
| **GitHub issues** | Auto-opened when a major jurisdiction is down > 24h |
| **Email digest** | Sent on status changes and every Monday baseline |
| **CSV update** | `data/sipp_courts.csv` updated with latest connectivity on each run |

---

## Repository structure

```
sipp-monitor/
├── .github/
│   └── workflows/
│       └── sipp-daily-check.yml   # Main workflow
├── data/
│   ├── sipp_courts.csv            # Master court list (349 courts)
│   └── state.json                 # Persisted connectivity state (auto-updated)
├── reports/
│   ├── latest.md                  # Most recent report
│   └── YYYY-MM-DD.md             # Dated daily reports
├── scripts/
│   └── check_connectivity.py      # Core checker + report generator
├── requirements.txt
└── README.md
```

---

## Setup

### 1. Fork / clone this repo

```bash
git clone https://github.com/epireve/sipp-monitor.git
cd sipp-monitor
```

### 2. Configure email delivery

The workflow supports two email backends. Add the appropriate secrets in **Settings → Secrets and variables → Actions**:

#### Option A — SendGrid (recommended)

| Secret | Value |
|--------|-------|
| `SENDGRID_API_KEY` | Your SendGrid API key |

Set your verified sender email in the workflow env: `from_email = "sipp-monitor@yourdomain.com"`

#### Option B — Gmail App Password (fallback)

| Secret | Value |
|--------|-------|
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | 16-char app-specific password ([generate here](https://myaccount.google.com/apppasswords)) |

### 3. Verify `NOTIFY_EMAIL`

In `.github/workflows/sipp-daily-check.yml`, confirm:

```yaml
env:
  NOTIFY_EMAIL: i@firdaus.my   # ← your email
```

### 4. Trigger a manual run

Go to **Actions → SIPP Daily Connectivity Check → Run workflow** and check `force_email: true` to verify email delivery works.

---

## Major jurisdictions monitored for 24h auto-issue

These courts trigger a GitHub issue when offline > 24 consecutive hours:

Jakarta (all 5 courts) · Surabaya · Bandung · Medan · Semarang · Makassar · Palembang · Pekanbaru · Banjarmasin · Pontianak · Samarinda · Balikpapan · Manado · Denpasar · Mataram · Kupang · Ambon · Jayapura · Yogyakarta · Malang · Bekasi · Tangerang · Bogor

---

## Email schedule

| Condition | Email sent? |
|-----------|-------------|
| Any court newly goes down | ✅ Always |
| Major jurisdiction > 24h down | ✅ Always (subject prefixed `🔴`) |
| Monday (weekly baseline) | ✅ Always |
| No changes, Tue–Fri | ❌ Skipped |
| Manual trigger with `force_email: true` | ✅ Always |

---

## Running locally

```bash
pip install -r requirements.txt

# Full check (no GitHub issues)
python scripts/check_connectivity.py --no-issues

# Custom paths
python scripts/check_connectivity.py \
  --courts-csv data/sipp_courts.csv \
  --output-dir reports \
  --state-file data/state.json \
  --no-issues
```

---

## Data sources

- Court list compiled from [PA Watampone national directory](https://www.pa-watampone.go.id/tautan-terkait/9-artikel/564-alamat-situs-pengadilan-negeri-se-indonesia)
- URL pattern: `https://sipp.pn-<shortname>.go.id` per Mahkamah Agung standard
- Baseline validation run: 3 July 2026 — 316/349 reachable (90.5%)

---

## License

MIT — data is derived from public Indonesian government web services.
