#!/usr/bin/env python3
"""
Study Tracker - Weekly Literature Digest
==========================================
Searches PubMed, bioRxiv/medRxiv, and Google Scholar for recent
publications matching configured queries, then sends an HTML
email digest.

Usage:
    python study_tracker.py                    # Run with config.yaml
    python study_tracker.py --config my.yaml   # Custom config
    python study_tracker.py --dry-run          # Print digest, don't email
"""

import argparse
import datetime
import hashlib
import html
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml


# ── Utilities ───────────────────────────────────────────────

def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


def paper_id(title, authors=""):
    """Generate a dedup key from title."""
    normalized = title.lower().strip()
    return hashlib.md5(normalized.encode()).hexdigest()


def days_ago(n):
    return datetime.date.today() - datetime.timedelta(days=n)


def safe_request(url, max_retries=3, delay=1.0):
    """Make an HTTP request with retries and polite delays."""
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "StudyTracker/1.0 (Academic Literature Digest)"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                print(f"  ⚠ Request failed after {max_retries} attempts: {e}")
                return None


# ── PubMed Search ───────────────────────────────────────────

def search_pubmed(queries, days_back=7, max_results=20):
    """Search PubMed via NCBI E-utilities."""
    base_search = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    base_fetch = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

    api_key = os.environ.get("NCBI_API_KEY", "")
    api_param = f"&api_key={api_key}" if api_key else ""

    min_date = days_ago(days_back).strftime("%Y/%m/%d")
    max_date = datetime.date.today().strftime("%Y/%m/%d")

    all_pmids = set()
    papers = []

    for query in queries:
        print(f"  PubMed: searching '{query}'...")
        search_url = (
            f"{base_search}?db=pubmed&term={urllib.parse.quote(query)}"
            f"&mindate={min_date}&maxdate={max_date}&datetype=edat"
            f"&retmax={max_results}&retmode=json&sort=date"
            f"{api_param}"
        )

        data = safe_request(search_url)
        if not data:
            continue

        try:
            result = json.loads(data)
            pmids = result.get("esearchresult", {}).get("idlist", [])
        except (json.JSONDecodeError, KeyError):
            continue

        new_pmids = [p for p in pmids if p not in all_pmids]
        all_pmids.update(new_pmids)

        if not new_pmids:
            continue

        # Fetch details for new PMIDs
        fetch_url = (
            f"{base_fetch}?db=pubmed&id={','.join(new_pmids)}"
            f"&retmode=xml{api_param}"
        )

        xml_data = safe_request(fetch_url)
        if not xml_data:
            continue

        # Be polite to NCBI (max 3 requests/sec without API key)
        time.sleep(0.4 if api_key else 0.5)

        try:
            root = ET.fromstring(xml_data)
            for article in root.findall(".//PubmedArticle"):
                paper = _parse_pubmed_article(article)
                if paper:
                    paper["_query"] = query
                    papers.append(paper)
        except ET.ParseError:
            continue

    return papers


def _parse_pubmed_article(article):
    """Parse a single PubmedArticle XML element."""
    try:
        medline = article.find(".//MedlineCitation")
        pmid = medline.findtext("PMID", "")
        art = medline.find("Article")

        title = art.findtext("ArticleTitle", "No title")

        # Authors
        authors = []
        for author in art.findall(".//Author"):
            last = author.findtext("LastName", "")
            first = author.findtext("ForeName", "")
            if last:
                authors.append(f"{last} {first}".strip())

        # Abstract
        abstract_parts = []
        for abstract_text in art.findall(".//AbstractText"):
            label = abstract_text.get("Label", "")
            text = abstract_text.text or ""
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)
        abstract = " ".join(abstract_parts)

        # Journal
        journal = art.findtext(".//Title", "")

        # Date
        pub_date = art.find(".//PubDate")
        date_str = ""
        if pub_date is not None:
            year = pub_date.findtext("Year", "")
            month = pub_date.findtext("Month", "")
            day = pub_date.findtext("Day", "")
            date_str = f"{year} {month} {day}".strip()

        # DOI
        doi = ""
        for eid in article.findall(".//ArticleId"):
            if eid.get("IdType") == "doi":
                doi = eid.text or ""

        return {
            "source": "PubMed",
            "pmid": pmid,
            "title": title,
            "authors": authors[:5],  # First 5 authors
            "abstract": abstract[:500],
            "journal": journal,
            "date": date_str,
            "doi": doi,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
    except Exception:
        return None


# ── bioRxiv / medRxiv Search ───────────────────────────────

def search_biorxiv(queries, collections=None, days_back=7, max_results=20):
    """Search bioRxiv and medRxiv via their API."""
    base_url = "https://api.biorxiv.org/details"
    start_date = days_ago(days_back).strftime("%Y-%m-%d")
    end_date = datetime.date.today().strftime("%Y-%m-%d")

    papers = []
    seen_dois = set()

    for server in ["biorxiv", "medrxiv"]:
        print(f"  {server}: fetching recent papers...")
        cursor = 0
        batch_size = 100
        server_papers = []

        # Fetch recent papers in batches
        while cursor < 500:  # Safety limit
            url = f"{base_url}/{server}/{start_date}/{end_date}/{cursor}"
            data = safe_request(url)
            if not data:
                break

            try:
                result = json.loads(data)
                collection = result.get("collection", [])
                if not collection:
                    break
                server_papers.extend(collection)
                cursor += batch_size

                # If we got fewer than batch_size, we're done
                if len(collection) < batch_size:
                    break
            except (json.JSONDecodeError, KeyError):
                break

            time.sleep(0.3)

        # Filter by queries and collections
        for paper in server_papers:
            title = paper.get("title", "").lower()
            abstract = paper.get("abstract", "").lower()
            category = paper.get("category", "").lower()
            doi = paper.get("doi", "")

            if doi in seen_dois:
                continue

            # Check collection filter
            if collections:
                col_match = any(
                    c.lower().replace("-", " ") in category.replace("-", " ")
                    for c in collections
                )
            else:
                col_match = True

            # Check query match
            query_match = False
            matched_query = ""
            for query in queries:
                terms = query.lower().split()
                if all(t in title or t in abstract for t in terms):
                    query_match = True
                    matched_query = query
                    break

            if query_match or (col_match and not queries):
                if not query_match and not any(
                    all(t in title or t in abstract for t in q.lower().split())
                    for q in queries
                ):
                    continue

                seen_dois.add(doi)
                authors_raw = paper.get("authors", "")
                authors = [a.strip() for a in authors_raw.split(";")][:5]

                papers.append({
                    "source": f"{server}",
                    "title": paper.get("title", "No title"),
                    "authors": authors,
                    "abstract": paper.get("abstract", "")[:500],
                    "journal": f"{server} preprint",
                    "date": paper.get("date", ""),
                    "doi": doi,
                    "url": f"https://doi.org/{doi}" if doi else "",
                    "category": paper.get("category", ""),
                    "_query": matched_query,
                })

                if len(papers) >= max_results * len(queries):
                    break

    return papers


# ── Google Scholar Search (via SerpAPI) ─────────────────────

def search_google_scholar(queries, days_back=7, max_results=20):
    """Search Google Scholar via SerpAPI."""
    api_key = os.environ.get("SERPAPI_KEY", "")
    if not api_key:
        print("  Google Scholar: SERPAPI_KEY not set, skipping.")
        return []

    base_url = "https://serpapi.com/search.json"
    papers = []
    seen_titles = set()

    # SerpAPI uses "as_ylo" for year filter (no exact day filter)
    current_year = datetime.date.today().year

    for query in queries:
        print(f"  Google Scholar: searching '{query}'...")
        params = urllib.parse.urlencode({
            "engine": "google_scholar",
            "q": query,
            "as_ylo": current_year,
            "num": min(max_results, 20),
            "api_key": api_key,
        })

        data = safe_request(f"{base_url}?{params}")
        if not data:
            continue

        try:
            result = json.loads(data)
            for item in result.get("organic_results", []):
                title = item.get("title", "No title")
                title_lower = title.lower().strip()

                if title_lower in seen_titles:
                    continue
                seen_titles.add(title_lower)

                # Parse authors from snippet
                info = item.get("publication_info", {})
                authors_str = info.get("summary", "")
                authors = [a.strip() for a in authors_str.split(",")][:3]

                papers.append({
                    "source": "Google Scholar",
                    "title": title,
                    "authors": authors,
                    "abstract": item.get("snippet", "")[:500],
                    "journal": "",
                    "date": str(current_year),
                    "doi": "",
                    "url": item.get("link", ""),
                    "_query": query,
                })
        except (json.JSONDecodeError, KeyError):
            continue

        time.sleep(1.0)  # Be polite to SerpAPI

    return papers


# ── Deduplication ──────────────────────────────────────────

def deduplicate(papers):
    """Remove duplicate papers based on title similarity."""
    seen = {}
    unique = []

    for paper in papers:
        pid = paper_id(paper["title"])
        if pid not in seen:
            seen[pid] = paper
            unique.append(paper)
        else:
            # Prefer PubMed > bioRxiv > Google Scholar
            priority = {"PubMed": 3, "biorxiv": 2, "medrxiv": 2, "Google Scholar": 1}
            existing = seen[pid]
            if priority.get(paper["source"], 0) > priority.get(existing["source"], 0):
                idx = unique.index(existing)
                unique[idx] = paper
                seen[pid] = paper

    return unique


# ── Email Formatting ───────────────────────────────────────

def format_email_html(papers, config):
    """Generate a styled HTML email digest."""
    today = datetime.date.today().strftime("%B %d, %Y")
    days_back = config.get("settings", {}).get("days_back", 7)

    # Group by source
    by_source = {}
    for p in papers:
        src = p["source"]
        by_source.setdefault(src, []).append(p)

    papers_html = ""

    for source in ["PubMed", "biorxiv", "medrxiv", "Google Scholar"]:
        source_papers = by_source.get(source, [])
        if not source_papers:
            continue

        source_label = {
            "biorxiv": "bioRxiv",
            "medrxiv": "medRxiv",
        }.get(source, source)

        papers_html += f"""
        <tr><td style="padding: 20px 0 8px 0;">
            <h2 style="color: #2563eb; font-size: 18px; margin: 0; border-bottom: 2px solid #2563eb; padding-bottom: 6px;">
                {source_label} ({len(source_papers)} papers)
            </h2>
        </td></tr>
        """

        for p in source_papers:
            title_safe = html.escape(p["title"])
            url = p.get("url", "")
            authors = ", ".join(p.get("authors", [])[:5])
            if len(p.get("authors", [])) > 5:
                authors += " et al."
            authors_safe = html.escape(authors)
            journal_safe = html.escape(p.get("journal", ""))
            date_safe = html.escape(p.get("date", ""))
            abstract_safe = html.escape(p.get("abstract", "")[:300])
            if len(p.get("abstract", "")) > 300:
                abstract_safe += "..."
            query_safe = html.escape(p.get("_query", ""))
            doi = p.get("doi", "")

            title_link = f'<a href="{url}" style="color: #1e40af; text-decoration: none;">{title_safe}</a>' if url else title_safe

            meta_parts = []
            if journal_safe:
                meta_parts.append(f"<em>{journal_safe}</em>")
            if date_safe:
                meta_parts.append(date_safe)
            if doi:
                meta_parts.append(f'DOI: <a href="https://doi.org/{doi}" style="color: #6b7280;">{doi}</a>')
            meta_line = " · ".join(meta_parts)

            papers_html += f"""
            <tr><td style="padding: 12px 0; border-bottom: 1px solid #e5e7eb;">
                <div style="font-size: 15px; font-weight: 600; line-height: 1.4; margin-bottom: 4px;">
                    {title_link}
                </div>
                <div style="font-size: 13px; color: #374151; margin-bottom: 4px;">
                    {authors_safe}
                </div>
                <div style="font-size: 12px; color: #6b7280; margin-bottom: 6px;">
                    {meta_line}
                </div>
                <div style="font-size: 13px; color: #4b5563; line-height: 1.5;">
                    {abstract_safe}
                </div>
                <div style="font-size: 11px; color: #9ca3af; margin-top: 4px;">
                    Matched: <em>{query_safe}</em>
                </div>
            </td></tr>
            """

    if not papers:
        papers_html = """
        <tr><td style="padding: 40px 20px; text-align: center; color: #6b7280;">
            <p style="font-size: 16px;">No new papers found this week.</p>
            <p style="font-size: 13px;">Consider broadening your search terms in config.yaml.</p>
        </td></tr>
        """

    email_html = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="margin: 0; padding: 0; background-color: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f3f4f6; padding: 20px 0;">
            <tr><td align="center">
                <table width="640" cellpadding="0" cellspacing="0" style="background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <!-- Header -->
                    <tr><td style="background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%); padding: 24px 30px;">
                        <h1 style="color: #ffffff; margin: 0; font-size: 22px;">📚 Weekly Literature Digest</h1>
                        <p style="color: #93c5fd; margin: 6px 0 0 0; font-size: 14px;">
                            {today} · Papers from the last {days_back} days
                        </p>
                    </td></tr>

                    <!-- Summary -->
                    <tr><td style="padding: 20px 30px 0 30px;">
                        <div style="background-color: #eff6ff; border-radius: 6px; padding: 14px 18px; font-size: 14px; color: #1e40af;">
                            Found <strong>{len(papers)}</strong> papers across
                            <strong>{len(by_source)}</strong> source{'s' if len(by_source) != 1 else ''}
                        </div>
                    </td></tr>

                    <!-- Papers -->
                    <tr><td style="padding: 10px 30px 30px 30px;">
                        <table width="100%" cellpadding="0" cellspacing="0">
                            {papers_html}
                        </table>
                    </td></tr>

                    <!-- Footer -->
                    <tr><td style="background-color: #f9fafb; padding: 16px 30px; border-top: 1px solid #e5e7eb;">
                        <p style="font-size: 12px; color: #9ca3af; margin: 0; text-align: center;">
                            Generated by Study Tracker · Edit search terms in config.yaml
                        </p>
                    </td></tr>
                </table>
            </td></tr>
        </table>
    </body>
    </html>
    """

    return email_html


def format_email_text(papers, config):
    """Generate a plain text fallback."""
    today = datetime.date.today().strftime("%B %d, %Y")
    days_back = config.get("settings", {}).get("days_back", 7)

    lines = [
        f"Weekly Literature Digest - {today}",
        f"Papers from the last {days_back} days",
        f"Found {len(papers)} papers",
        "=" * 60,
        "",
    ]

    for p in papers:
        lines.append(f"[{p['source']}] {p['title']}")
        authors = ", ".join(p.get("authors", [])[:5])
        if authors:
            lines.append(f"  Authors: {authors}")
        if p.get("journal"):
            lines.append(f"  Journal: {p['journal']}")
        if p.get("url"):
            lines.append(f"  URL: {p['url']}")
        if p.get("abstract"):
            lines.append(f"  Abstract: {p['abstract'][:200]}...")
        lines.append("")

    return "\n".join(lines)


# ── Email Sending ──────────────────────────────────────────

def send_email(subject, html_body, text_body, config):
    """Send the digest email via SMTP."""
    email_config = config.get("email", {})

    sender = os.environ.get("SENDER_EMAIL", email_config.get("sender", ""))
    recipient = os.environ.get("RECIPIENT_EMAIL", email_config.get("recipient", ""))
    password = os.environ.get("EMAIL_PASSWORD", "")
    smtp_server = email_config.get("smtp_server", "smtp.gmail.com")
    smtp_port = email_config.get("smtp_port", 587)

    if not all([sender, recipient, password]):
        print("⚠ Email credentials not fully configured.")
        print("  Set SENDER_EMAIL, RECIPIENT_EMAIL, and EMAIL_PASSWORD.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Study Tracker <{sender}>"
    msg["To"] = recipient

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=context)
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        print(f"✅ Digest sent to {recipient}")
        return True
    except Exception as e:
        print(f"❌ Failed to send email: {e}")
        return False


# ── Main ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Weekly Literature Digest")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--dry-run", action="store_true", help="Print digest without emailing")
    parser.add_argument("--output", help="Save HTML digest to file")
    args = parser.parse_args()

    config = load_config(args.config)
    settings = config.get("settings", {})
    days_back = settings.get("days_back", 7)
    max_results = settings.get("max_results_per_query", 20)

    print(f"🔬 Study Tracker - Searching last {days_back} days\n")

    all_papers = []

    # PubMed
    pubmed_config = config.get("pubmed", {})
    if pubmed_config.get("queries"):
        print("📖 Searching PubMed...")
        papers = search_pubmed(
            pubmed_config["queries"],
            days_back=days_back,
            max_results=max_results,
        )
        print(f"   Found {len(papers)} papers\n")
        all_papers.extend(papers)

    # bioRxiv/medRxiv
    biorxiv_config = config.get("biorxiv", {})
    if biorxiv_config.get("queries"):
        print("🧬 Searching bioRxiv/medRxiv...")
        papers = search_biorxiv(
            biorxiv_config["queries"],
            collections=biorxiv_config.get("collections"),
            days_back=days_back,
            max_results=max_results,
        )
        print(f"   Found {len(papers)} papers\n")
        all_papers.extend(papers)

    # Google Scholar
    scholar_config = config.get("google_scholar", {})
    if scholar_config.get("queries"):
        print("🎓 Searching Google Scholar...")
        papers = search_google_scholar(
            scholar_config["queries"],
            days_back=days_back,
            max_results=max_results,
        )
        print(f"   Found {len(papers)} papers\n")
        all_papers.extend(papers)

    # Deduplicate
    if settings.get("deduplicate", True):
        before = len(all_papers)
        all_papers = deduplicate(all_papers)
        print(f"📋 {len(all_papers)} unique papers (removed {before - len(all_papers)} duplicates)\n")

    # Format
    today = datetime.date.today().strftime("%B %d, %Y")
    subject = f"📚 Literature Digest: {len(all_papers)} papers - {today}"
    html_body = format_email_html(all_papers, config)
    text_body = format_email_text(all_papers, config)

    # Output
    if args.output:
        Path(args.output).write_text(html_body)
        print(f"💾 Saved digest to {args.output}")

    if args.dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN - Email would contain:")
        print("=" * 60)
        print(f"Subject: {subject}\n")
        print(text_body)
        return

    send_email(subject, html_body, text_body, config)


if __name__ == "__main__":
    main()
