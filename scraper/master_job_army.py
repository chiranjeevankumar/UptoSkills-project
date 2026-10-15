import sys
import os
import re
import time
import random
import json
import argparse
import urllib.parse
import urllib.request
import urllib.error
from itertools import zip_longest
from datetime import datetime
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Ensure UTF-8 output on Windows terminal
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


TARGET_PLATFORMS = [
    {"name": "Naukri", "query_suffix": "site:naukri.com", "domains": ["naukri.com"]},
    {"name": "Indeed", "query_suffix": "site:indeed.com", "domains": ["indeed.com"]},
    {"name": "Instahyre", "query_suffix": "site:instahyre.com", "domains": ["instahyre.com"]},
    {"name": "Foundit", "query_suffix": "site:foundit.in", "domains": ["foundit.in", "monsterindia.com"]},
    {"name": "Wellfound", "query_suffix": "site:wellfound.com", "domains": ["wellfound.com"]},
    {"name": "GitHub Careers", "query_suffix": "careers site:github.com", "domains": ["github.com"]},
    {"name": "Google Jobs", "query_suffix": "jobs apply", "domains": []},
]


def extract_hr_contact(text: str) -> str:
    if not text:
        return "Not Disclosed"

    contacts = []

    emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', text)
    clean_emails = [
        e.strip().rstrip(".") for e in set(emails)
        if not any(ign in e.lower() for ign in ["example.com", "indeed.com", "sentry.io", "w3.org", "schema.org"])
    ]
    if clean_emails:
        contacts.append(f"Email: {', '.join(clean_emails[:2])}")

    phone_patterns = [
        r'\+91[\-\s]?([6-9]\d{9})\b',
        r'\b([6-9]\d{2}[\-\s]\d{3}[\-\s]\d{4})\b',
        r'(?:call|contact|whatsapp|ph|mobile|hr)[:\s-]+(?:\+?91[\-\s]?)?([6-9]\d{9})\b',
    ]
    raw_matches = []
    for pat in phone_patterns:
        raw_matches.extend(re.findall(pat, text, re.IGNORECASE))

    clean_phones = []
    seen = set()
    for p in raw_matches:
        digits = re.sub(r'[\-\s]', '', p)
        if len(digits) == 10 and digits[0] in "6789" and digits not in seen:
            seen.add(digits)
            clean_phones.append(digits)

    if clean_phones:
        contacts.append(f"Phone/WhatsApp: {', '.join(clean_phones[:2])}")

    recruiter_match = re.search(r'(?:contact\s+(?:person|hr)|hr\s+name)[:\s]+([A-Za-z\s]{3,25})', text, re.IGNORECASE)
    if recruiter_match:
        contacts.append(f"HR: {recruiter_match.group(1).strip()}")

    return " | ".join(contacts) if contacts else "Not Disclosed"


def infer_role(title: str, query: str = "") -> str:
    if not title:
        return query.title() if query else "Intern / Trainee"

    t_lower = title.lower()
    if "ai" in t_lower or "artificial intelligence" in t_lower:
        if "intern" in t_lower or "trainee" in t_lower:
            return "AI / ML Intern"
        return "AI Engineer"
    elif "machine learning" in t_lower or "ml" in t_lower:
        return "Machine Learning Intern"
    elif "data science" in t_lower or "data scientist" in t_lower:
        return "Data Science Intern"
    elif "python" in t_lower:
        return "Python Developer Intern"
    elif "software" in t_lower or "developer" in t_lower:
        return "Software Developer Intern"
    elif "research" in t_lower:
        return "AI Research Intern"
    elif "intern" in t_lower:
        return "Intern"
    return title.split("-")[0].split("|")[0].strip()


def calculate_lead_score(lead: dict) -> int:
    score = 0
    if lead.get("COMPANY") and lead["COMPANY"] not in ["Company", "Unknown"]:
        score += 15
    if lead.get("JOB TITLE"):
        score += 15
    if lead.get("ROLE"):
        score += 10
    if lead.get("AREA / LOCATION") and lead["AREA / LOCATION"] not in ["Not Disclosed", "Unknown"]:
        score += 10

    sal = str(lead.get("SALARY") or "").lower()
    if sal and sal not in ["none", "not disclosed", "not disclosed p.a.", ""]:
        score += 15
    elif sal:
        score += 5

    hr = str(lead.get("HR CONTACT") or "").lower()
    if hr and "not disclosed" not in hr and hr != "none":
        if any(c in hr for c in ["email:", "phone", "recruiter:"]):
            score += 25
        else:
            score += 20
    else:
        score += 5

    desc = str(lead.get("FULL DESCRIPTION") or "")
    if len(desc) > 200:
        score += 10
    elif len(desc) > 50:
        score += 5

    return min(100, score)

SERPER_URL = "https://google.serper.dev/search"


def harvest_links_for_platform(base_query: str, platform: dict) -> list:
    api_key = os.environ.get("SERPER_API_KEY")
    if not api_key:
        raise RuntimeError("SERPER_API_KEY is not set on the scraper service")

    q = f"{base_query} {platform['query_suffix']}".strip()
    print(f"[*] Searching for: '{q}'...")

    body = json.dumps({"q": q, "num": 10, "gl": "in"}).encode("utf-8")
    req = urllib.request.Request(
        SERPER_URL,
        data=body,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 402, 403, 429):
            raise RuntimeError(f"Search API error {e.code}: API key invalid or credits exhausted")
        print(f"    [-] Search failed for {platform['name']}: HTTP {e.code}")
        return []
    except Exception as e:
        print(f"    [-] Search failed for {platform['name']}: {e}")
        return []

    found_links = []
    seen_urls = set()
    for item in data.get("organic", []):
        url = item.get("link") or ""
        title = (item.get("title") or "").strip()
        lower_u = url.lower()
        if not url or url in seen_urls:
            continue
        if platform["domains"] and not any(d in lower_u for d in platform["domains"]):
            continue
        if any(noise in lower_u for noise in ["/privacy", "/terms", "/about", "/press", "login", "signup", "/faq"]):
            continue
        seen_urls.add(url)
        found_links.append({"url": url, "title": title or "Job Listing", "platform": platform["name"]})

    print(f"    [+] Harvested {len(found_links)} leads for {platform['name']}")
    return found_links


def clean_job_url(raw_url: str) -> str:
    if not raw_url or not isinstance(raw_url, str):
        return ""
    try:
        parsed = urllib.parse.urlparse(raw_url)
        netloc = parsed.netloc.lower()
        if "indeed." in netloc:
            jk_match = re.search(r'[?&]jk=([a-zA-Z0-9]+)', raw_url)
            if jk_match:
                return f"https://{netloc}/viewjob?jk={jk_match.group(1)}"
            return raw_url
        elif "naukri.com" in netloc:
            return f"https://www.naukri.com{parsed.path}"
        elif "glassdoor." in netloc:
            qs = urllib.parse.parse_qs(parsed.query)
            jl_id = qs.get("jl", [None])[0] or qs.get("jobListingId", [None])[0]
            if jl_id:
                return f"https://{netloc}{parsed.path}?jl={jl_id}"
            return f"https://{netloc}{parsed.path}"
        else:
            qs = urllib.parse.parse_qs(parsed.query)
            filtered_qs = {k: v for k, v in qs.items() if not k.lower().startswith("utm_") and k.lower() not in ["fbclid", "gclid", "ref", "source", "src", "sid", "xp", "px"]}
            new_query = urllib.parse.urlencode(filtered_qs, doseq=True)
            return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
    except Exception:
        return raw_url


def parse_target_location(query: str) -> str:
    q_lower = query.lower().strip()
    if any(r in q_lower for r in ["remote", "work from home", "wfh", "anywhere"]):
        return "Remote"

    KNOWN_LOCATIONS = [
        "visakhapatnam", "vizag", "hyderabad", "bangalore", "bengaluru",
        "mumbai", "pune", "delhi", "noida", "gurgaon", "gurugram", "chennai",
        "kolkata", "ahmedabad", "patna", "jaipur", "chandigarh", "kochi",
        "coimbatore", "indore", "lucknow", "bhopal", "vijayawada", "tirupati",
        "andhra pradesh", "telangana", "karnataka", "tamil nadu", "maharashtra",
        "bihar", "uttar pradesh", "gujarat", "kerala", "west bengal", "goa"
    ]
    for loc in KNOWN_LOCATIONS:
        if re.search(rf'\b{re.escape(loc)}\b', q_lower):
            return loc.title()

    m = re.search(r'\b(?:near|in|at|around)\s+([a-zA-Z\s]{2,25})', q_lower)
    if m:
        cand = m.group(1).split()[0].strip().title()
        if cand:
            return cand

    stop_words = [
        "ai", "ml", "intern", "internship", "internships", "jobs", "job",
        "fresher", "freshers", "developer", "engineer", "trainee", "near",
        "in", "at", "for", "hiring", "openings", "opening", "remote", "wfh",
        "junior", "senior", "lead", "entry", "level", "urgent"
    ]
    tokens = [w.title() for w in q_lower.split() if w not in stop_words]
    if tokens:
        return " ".join(tokens)
    return "India"


def extract_location(page, item_json: dict, fallback_text: str, query: str) -> str:
    target_loc = parse_target_location(query)

    if item_json:
        loc = item_json.get("jobLocation")
        if isinstance(loc, dict):
            addr = loc.get("address")
            if isinstance(addr, dict):
                parts = [addr.get("streetAddress"), addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
                clean_parts = [str(p).strip() for p in parts if p and str(p).strip()]
                if clean_parts:
                    return ", ".join(clean_parts)
            elif isinstance(addr, str) and addr.strip():
                return addr.strip()
            elif loc.get("name"):
                return str(loc.get("name")).strip()
        elif isinstance(loc, list) and len(loc) > 0:
            first_loc = loc[0]
            if isinstance(first_loc, dict):
                addr = first_loc.get("address")
                if isinstance(addr, dict):
                    parts = [addr.get("addressLocality"), addr.get("addressRegion")]
                    clean_parts = [str(p).strip() for p in parts if p and str(p).strip()]
                    if clean_parts:
                        return ", ".join(clean_parts)
                elif isinstance(addr, str) and addr.strip():
                    return addr.strip()
        if item_json.get("jobLocationType") == "TELECOMMUTE":
            return "Remote / Work from Home"

    try:
        dom_selectors = [
            '[data-testid*="job-location"]', '[data-testid*="location"]',
            '[class*="companyLocation"]', '[class*="loc-wrap"]',
            '[class*="job-location"]', '[class*="location"]', '.loc'
        ]
        for sel in dom_selectors:
            loc_el = page.query_selector(sel)
            if loc_el:
                txt = loc_el.inner_text().strip().replace("\n", ", ")
                if txt and len(txt) < 80 and not any(k in txt.lower() for k in ["login", "filter", "sign in", "post a job"]):
                    return txt
    except Exception:
        pass

    comb = f"{fallback_text}".lower()
    if any(k in comb for k in ["remote", "work from home", "wfh"]):
        return "Remote / Anywhere"

    if target_loc.lower() in comb:
        return target_loc

    return target_loc


def extract_page_contacts(page, page_text: str, company: str) -> str:
    contacts = []

    try:
        for a in page.query_selector_all('a[href^="mailto:"]'):
            href = a.get_attribute("href") or ""
            email = href.replace("mailto:", "").split("?")[0].strip()
            if email and "@" in email and not any(ign in email.lower() for ign in ["example.com", "sentry.io", "w3.org", "schema.org"]):
                contacts.append(f"Email: {email}")
                break
    except Exception:
        pass

    try:
        for a in page.query_selector_all('a[href^="tel:"]'):
            href = a.get_attribute("href") or ""
            tel = href.replace("tel:", "").strip()
            if tel and len(tel) >= 10:
                contacts.append(f"Phone: {tel}")
                break
    except Exception:
        pass

    try:
        recruiter_selectors = [
            '.rec-name', '.recruiter-name', '[class*="recruiter"] a',
            '[class*="posted-by"]', '.recruiter-info', '.rec-details',
            '[data-testid*="recruiter"]'
        ]
        for sel in recruiter_selectors:
            el = page.query_selector(sel)
            if el:
                rec_text = el.inner_text().strip()
                if rec_text and len(rec_text) < 40 and not any(x in rec_text.lower() for x in ["apply", "login", "register", "view"]):
                    contacts.append(f"Recruiter: {rec_text}")
                    break
    except Exception:
        pass

    regex_res = extract_hr_contact(page_text)
    if regex_res and regex_res != "Not Disclosed":
        contacts.append(regex_res)

    if contacts:
        unique = []
        seen = set()
        for c in contacts:
            for p in c.split("|"):
                p_str = p.strip()
                if p_str and p_str.lower() not in seen:
                    seen.add(p_str.lower())
                    unique.append(p_str)
        if unique:
            return " | ".join(unique[:3])

    if company and company not in ["Company", "Unknown", "None"]:
        clean_name = re.sub(r'[^a-zA-Z0-9]', '', company.lower())
        if 3 <= len(clean_name) <= 20:
            return f"Inferred (unverified): careers@{clean_name}.com"

    return "Not Disclosed"


def scrape_job_page(page, url: str, platform_name: str, query: str = "", fallback_title: str = "") -> dict:
    lead = {
        "SCORE": 0,
        "COMPANY": None,
        "JOB TITLE": None,
        "ROLE": None,
        "AREA / LOCATION": parse_target_location(query),
        "SALARY": "Not Disclosed",
        "HR CONTACT": "Not Disclosed",
        "VERIFICATION": "Unverified",
        "STAGE": "Discovered",
        "SOURCE": platform_name,
        "DISCOVERED": datetime.now().strftime("%Y-%m-%d"),
        "JOB URL": clean_job_url(url),
        "FULL DESCRIPTION": ""
    }

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(2000)

        current_url = page.url
        if current_url and "google.com" not in current_url:
            lead["JOB URL"] = clean_job_url(current_url)

        json_posting = None

        for s in page.query_selector_all('script[type="application/ld+json"]'):
            try:
                item = json.loads(s.inner_text())
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    json_posting = item
                    lead["JOB TITLE"] = item.get("title")
                    lead["COMPANY"] = item.get("hiringOrganization", {}).get("name")
                    
                    if item.get("datePosted"):
                        lead["DISCOVERED"] = str(item.get("datePosted"))[:10]

                    sal = item.get("baseSalary") or item.get("estimatedSalary")
                    if isinstance(sal, dict):
                        v = sal.get("value", {})
                        curr = sal.get("currency", "INR")
                        if isinstance(v, dict):
                            lead["SALARY"] = f"{curr} {v.get('minValue', '')} - {v.get('maxValue', '')} {v.get('unitText', '')}".strip()
                        elif v:
                            lead["SALARY"] = f"{curr} {v}".strip()

                    raw_desc = item.get("description", "")
                    if raw_desc:
                        soup = BeautifulSoup(raw_desc, "html.parser")
                        lead["FULL DESCRIPTION"] = soup.get_text(separator="\n").strip()
                    break
            except Exception:
                continue

        if not lead["JOB TITLE"]:
            t_el = page.query_selector('h1, [data-testid*="title"], [class*="job-title"], [class*="jd-header-title"]')
            if t_el: lead["JOB TITLE"] = t_el.inner_text().strip()

        if not lead["JOB TITLE"] and fallback_title:
            lead["JOB TITLE"] = fallback_title

        if not lead["COMPANY"]:
            c_el = page.query_selector('[data-testid*="company"], [class*="company-name"], [class*="employer-name"], [class*="comp-name"]')
            if c_el:
                lead["COMPANY"] = c_el.inner_text().splitlines()[0].strip()

        if not lead["COMPANY"] and fallback_title:
            if " at " in fallback_title:
                lead["COMPANY"] = fallback_title.split(" at ")[-1].split("-")[0].strip()
            elif " hiring " in fallback_title:
                lead["COMPANY"] = fallback_title.split(" hiring ")[0].strip()
            elif " - " in fallback_title:
                parts = fallback_title.split(" - ")
                if len(parts) >= 2:
                    lead["COMPANY"] = parts[1].strip()

        if lead["SALARY"] == "Not Disclosed":
            s_el = page.query_selector('[class*="salary"], [data-testid*="salary"], [class*="jhc__salary"]')
            if s_el and s_el.inner_text().strip():
                lead["SALARY"] = s_el.inner_text().replace("\n", " ").strip()

        if not lead["FULL DESCRIPTION"]:
            d_el = page.query_selector('#jobDescriptionText, [class*="job-desc"], [class*="description"], section')
            if d_el:
                lead["FULL DESCRIPTION"] = d_el.inner_text().strip()

        if not lead["FULL DESCRIPTION"]:
            try:
                body_text = page.inner_text("body")
                lead["FULL DESCRIPTION"] = body_text[:3000].strip()
            except Exception:
                pass

        full_text = f"{lead['FULL DESCRIPTION']} {lead['JOB TITLE'] or ''}"

        if lead["SALARY"] == "Not Disclosed":
            sal_match = re.search(r'(?:₹|Rs\.?|INR)\s*[\d,]+(?:\s*-\s*[\d,]+)?(?:\s*(?:per month|p\.m\.|pm|per annum|p\.a\.|pa|a year|a month|month|yr|lpa|ctc))?', full_text, re.IGNORECASE)
            if sal_match:
                lead["SALARY"] = sal_match.group(0).strip()
            else:
                stipend_match = re.search(r'(?:stipend|salary)[:\s]+(?:₹|Rs\.?|INR)?\s*[\d,]+(?:\s*-\s*[\d,]+)?(?:\s*(?:per month|p\.m\.|pm|month|lpa))?', full_text, re.IGNORECASE)
                if stipend_match:
                    lead["SALARY"] = stipend_match.group(0).strip()

        lead["AREA / LOCATION"] = extract_location(
            page=page,
            item_json=json_posting,
            fallback_text=f"{lead['JOB TITLE']} {lead['COMPANY']} {lead['FULL DESCRIPTION'][:800]}",
            query=query
        )

        lead["HR CONTACT"] = extract_page_contacts(page, full_text, lead["COMPANY"])
        lead["ROLE"] = infer_role(lead["JOB TITLE"], query)

        if lead["JOB TITLE"] and lead["COMPANY"]:
            lead["VERIFICATION"] = "Verified"
        elif lead["JOB TITLE"]:
            lead["VERIFICATION"] = "Partially Verified"
        else:
            lead["VERIFICATION"] = "Unverified / Expired"

        lead["SCORE"] = calculate_lead_score(lead)
        return lead

    except Exception as e:
        lead["VERIFICATION"] = "Error"
        lead["JOB TITLE"] = lead["JOB TITLE"] or fallback_title
        lead["ROLE"] = infer_role(lead["JOB TITLE"], query)
        lead["AREA / LOCATION"] = parse_target_location(query)
        lead["FULL DESCRIPTION"] = f"Extraction error: {str(e)[:150]}"
        lead["SCORE"] = calculate_lead_score(lead)
        return lead


def is_valid_lead(lead: dict) -> bool:
    title = str(lead.get("JOB TITLE") or "").strip()
    comp = str(lead.get("COMPANY") or "").strip()
    desc = str(lead.get("FULL DESCRIPTION") or "").strip()
    verif = str(lead.get("VERIFICATION") or "").strip()

    if not title or title.lower() in ["job listing", "none", "nan", "error", "untitled", "access denied", "security check"]:
        return False
    if not comp or comp.lower() in ["none", "nan", "unknown", "company"]:
        return False

    blocked_signatures = [
        "access denied", "verify you are human", "human verification",
        "just a moment", "checking your browser", "security check",
        "attention required", "403 forbidden", "404 not found",
        "page not found", "cloudflare", "captcha", "recaptcha",
        "unsupported browser", "job expired", "job is no longer available",
        "please verify", "bot detection", "pardon our interruption"
    ]
    comb = f"{title} {desc[:500]}".lower()
    for sig in blocked_signatures:
        if sig in comb:
            return False

    if verif in ["Error", "Unverified / Expired"]:
        return False

    if len(desc) < 25 or desc.startswith("Extraction error:"):
        return False

    return True


def run_master_job_army(search_query: str, headful: bool = False, limit: int = None):
    per_platform = []
    for plat in TARGET_PLATFORMS:
        per_platform.append(harvest_links_for_platform(search_query, plat))
        time.sleep(0.3)

    mixed = [item for group in zip_longest(*per_platform) for item in group if item]

    seen_h_urls = set()
    all_harvested_links = []
    for item in mixed:
        if item["url"] not in seen_h_urls:
            seen_h_urls.add(item["url"])
            all_harvested_links.append(item)

    if not all_harvested_links:
        return []

    if limit and len(all_harvested_links) > limit:
        all_harvested_links = all_harvested_links[:limit]

    leads = []
    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        try:
            browser = p.chromium.launch(channel="chrome", headless=not headful, args=launch_args)
        except Exception:
            browser = p.chromium.launch(headless=not headful, args=launch_args)

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="en-US"
        )

        def _block_heavy(route):
            if route.request.resource_type in ("image", "media", "font", "stylesheet"):
                route.abort()
            else:
                route.continue_()

        context.route("**/*", _block_heavy)
        page = context.new_page()

        for item in all_harvested_links:
            lead_data = scrape_job_page(
                page=page,
                url=item["url"],
                platform_name=item["platform"],
                query=search_query,
                fallback_title=item.get("title", "")
            )
            leads.append(lead_data)
            time.sleep(random.uniform(1.0, 2.0))

        browser.close()

    valid_leads = [l for l in leads if is_valid_lead(l)]
    export_leads = valid_leads if valid_leads else leads
    export_leads = sorted(export_leads, key=lambda x: x.get("SCORE", 0), reverse=True)

    return export_leads

if __name__ == "__main__":
    pass
