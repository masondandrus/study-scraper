# 📚 Study Tracker – Weekly Literature Digest

Automatically searches PubMed, bioRxiv/medRxiv, and Google Scholar for recent papers in your subfield, then emails you a formatted weekly digest.

Runs for **free** via GitHub Actions — no server needed.

---

## Quick Start

### 1. Create a GitHub Repository

```bash
git init study-tracker
cd study-tracker
# Copy all project files in, then:
git add .
git commit -m "Initial setup"
git remote add origin https://github.com/YOUR_USERNAME/study-tracker.git
git push -u origin main
```

### 2. Set Up Gmail App Password

You need a Gmail **App Password** (not your regular password):

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** if not already on
3. Go to [App Passwords](https://myaccount.google.com/apppasswords)
4. Create a new app password for "Mail"
5. Copy the 16-character password

### 3. Add GitHub Secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret | Required | Description |
|---|---|---|
| `SENDER_EMAIL` | ✅ | Your Gmail address |
| `RECIPIENT_EMAIL` | ✅ | Where to receive digests (can be same as sender) |
| `EMAIL_PASSWORD` | ✅ | Gmail App Password from step 2 |
| `NCBI_API_KEY` | Optional | [NCBI API key](https://ncbiinsights.ncbi.nlm.nih.gov/2017/11/02/new-api-keys-for-the-e-utilities/) — raises rate limit from 3 to 10 req/sec |
| `SERPAPI_KEY` | Optional | [SerpAPI key](https://serpapi.com/) for Google Scholar (free tier: 100 searches/month). Without this, Google Scholar is skipped. |

### 4. Customize Search Terms

Edit `config.yaml` to adjust your queries:

```yaml
pubmed:
  queries:
    - "stress enhanced fear learning"
    - "your custom query here"
```

### 5. Test It

Trigger a manual run: **Actions** tab → **Weekly Literature Digest** → **Run workflow**

Or test locally:

```bash
pip install -r requirements.txt

# Dry run (prints results, no email)
python study_tracker.py --dry-run

# Save HTML digest to file
python study_tracker.py --output digest.html --dry-run

# Full run with email (set env vars first)
export SENDER_EMAIL="you@gmail.com"
export RECIPIENT_EMAIL="you@gmail.com"
export EMAIL_PASSWORD="your-app-password"
python study_tracker.py
```

---

## Schedule

By default, the digest runs **every Monday at 8:00 AM Pacific**. To change this, edit the cron expression in `.github/workflows/weekly_digest.yml`:

```yaml
schedule:
  - cron: "0 15 * * 1"  # 15:00 UTC = 8:00 AM Pacific
```

Useful cron patterns:
- `"0 15 * * 1"` — Mondays at 8 AM Pacific
- `"0 15 * * 5"` — Fridays at 8 AM Pacific
- `"0 15 * * 1,4"` — Mondays and Thursdays

---

## How It Works

1. **PubMed** — Queries NCBI E-utilities API for papers published in the last 7 days matching your search terms. Uses PubMed's date filtering for precise results.

2. **bioRxiv/medRxiv** — Fetches recent preprints via the bioRxiv API, then filters locally by your search terms and collection categories (e.g., neuroscience).

3. **Google Scholar** — Uses SerpAPI to search Google Scholar (optional; requires free API key). Note: Scholar can only filter by year, not exact dates, so some older papers may appear.

4. **Deduplication** — Papers appearing in multiple sources are merged, preferring PubMed records.

5. **Email** — Sends a styled HTML digest via Gmail SMTP with paper titles, authors, abstracts, DOIs, and direct links.

---

## Project Structure

```
study-tracker/
├── .github/workflows/
│   └── weekly_digest.yml    # GitHub Actions workflow
├── config.yaml              # Search terms and settings
├── study_tracker.py         # Main script
├── requirements.txt         # Python dependencies
└── README.md
```

---

## Tips

- **PubMed search syntax**: You can use boolean operators (`AND`, `OR`, `NOT`), field tags (`[Title/Abstract]`, `[MeSH Terms]`), and wildcards. See the [PubMed search guide](https://pubmed.ncbi.nlm.nih.gov/help/).
- **NCBI API key**: Free and recommended. Increases rate limit from 3 to 10 requests per second. Get one at [NCBI](https://ncbiinsights.ncbi.nlm.nih.gov/2017/11/02/new-api-keys-for-the-e-utilities/).
- **SerpAPI free tier**: 100 searches/month is plenty for a weekly tracker with 5 queries.
- **Digest artifacts**: Each run saves the digest HTML as a GitHub Actions artifact (retained 30 days), so you can view past digests even if email fails.
