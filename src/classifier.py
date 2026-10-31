import re
from datetime import datetime

from src import db
from src.announcements import get_announcements

# Bump this whenever you edit RULES. Everything is then reclassified automatically.
RULES_VERSION = "2026.10.1"

PRIORITY_RANK = {"High": 3, "Medium": 2, "Low": 1}

# Order matters: the FIRST matching rule wins.
RULES = [
    # ---------- Routine paperwork first, so boilerplate never looks like news ----------
    {
        "id": "trading_window",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [r"trading window"],
        "reason": "Trading window notice",
    },
    {
        "id": "shareholding_pattern",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [r"shareholding pattern"],
        "reason": "Periodic shareholding pattern filing",
    },
    {
        "id": "insider_sast",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [
            r"regulation 7\(",
            r"regulation 29\(",
            r"\bsast\b",
            r"insider trading",
            r"prohibition of insider",
        ],
        "reason": "Insider-trading or takeover-code disclosure",
    },
    {
        "id": "newspaper",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [r"newspaper"],
        "reason": "Newspaper copy of an already-filed disclosure",
    },
    {
        "id": "share_certificates",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [r"share certificates?"],
        "reason": "Share certificate administration",
    },
    {
        "id": "compliance_filings",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [
            r"compliance certificate",
            r"certificate under regulation",
            r"regulation 74\(5\)",
            r"regulation 40\(9\)",
            r"regulation 13\(3\)",
            r"regulation 24a",
            r"reconciliation of share capital",
            r"investor (grievance|complaints)",
            r"secretarial compliance",
            r"related party transactions?",
            r"business responsibility|\bbrsr\b",
            r"deviation|variation.{0,30}(funds|proceeds)",
            r"utili[sz]ation of (funds|proceeds)",
            r"monitoring agency",
        ],
        "reason": "Periodic regulatory compliance filing",
    },
    {
        "id": "esop",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [r"\besops?\b", r"employee stock (option|purchase)", r"stock options?", r"\besos\b"],
        "reason": "Employee stock option administration",
    },
    {
        "id": "annual_report",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [r"annual report"],
        "reason": "Annual report filing",
    },
    # ---------- Quarterly results ----------
    {
        "id": "results_date",
        "category": "Quarterly Results",
        "priority": "Medium",
        "patterns": [
            r"intimation of board meeting",
            r"board meeting intimation",
            r"notice of board meeting",
            r"schedule of board meeting",
            r"board meeting (is )?scheduled",
        ],
        "also": [r"financial results?", r"quarter ended", r"half[- ]year", r"year ended"],
        "exclude": [r"outcome"],
        "reason": "Board meeting scheduled to consider results",
    },
    {
        "id": "results_support",
        "category": "Quarterly Results",
        "priority": "Medium",
        "patterns": [
            r"press release",
            r"media release",
            r"investor presentation",
            r"presentation",
            r"earnings call",
            r"conference call",
            r"transcript",
            r"audio",
        ],
        "also": [r"results", r"quarter", r"earnings"],
        "reason": "Supporting material for results (press release, presentation or call)",
    },
    {
        "id": "results",
        "category": "Quarterly Results",
        "priority": "High",
        "patterns": [
            r"financial results?",
            r"\b(un)?audited (standalone|consolidated|financial)",
            r"results for the (quarter|half|year|period)",
            r"(quarter|half[- ]year|year) ended",
        ],
        "reason": "Financial results filed",
    },
    # ---------- Shareholder meetings (after results, so board outcomes aren't swallowed) ----------
    {
        "id": "agm_logistics",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [
            r"annual general meeting",
            r"\bagm\b",
            r"e-?voting",
            r"scrutini[sz]er",
            r"voting results",
            r"postal ballot",
        ],
        "exclude": [r"outcome of board meeting"],
        "reason": "Shareholder meeting logistics",
    },
    # ---------- Substantive events ----------
    {
        "id": "mergers_acquisitions",
        "category": "M&A / Restructuring",
        "priority": "Medium",
        "patterns": [
            r"merger",
            r"amalgamation",
            r"demerger|de-merger",
            r"scheme of arrangement",
            r"slump sale",
            r"divest",
            r"acquisition",
            r"\bacquire[sd]?\b",
            r"takeover",
            r"open offer",
            r"joint venture",
            r"share purchase agreement",
            r"sale of (the )?(undertaking|business)",
            r"wholly[- ]owned subsidiary",
        ],
        "high_if": [
            r"merger",
            r"amalgamation",
            r"demerger|de-merger",
            r"scheme of arrangement",
            r"takeover",
            r"open offer",
            r"slump sale",
            r"divest",
        ],
        "reason": "Merger, acquisition or restructuring event",
    },
    {
        "id": "corporate_action",
        "category": "Corporate Action",
        "priority": "Medium",
        "patterns": [
            r"dividend",
            r"bonus (issue|shares?)",
            r"stock split|sub-?division",
            r"buy-?back",
            r"record date",
        ],
        "high_if": [r"buy-?back", r"bonus", r"stock split|sub-?division"],
        "reason": "Shareholder-return or capital-structure action",
    },
    {
        "id": "management_change",
        "category": "Management Change",
        "priority": "Medium",
        "patterns": [
            r"appointment",
            r"re-?appointment",
            r"resign",
            r"cessation",
            r"relinquish",
            r"change in (directorate|management|key managerial)",
            r"demise",
            r"retire",
        ],
        "also": [
            r"director",
            r"\bceo\b",
            r"chief",
            r"managing",
            r"\bcfo\b",
            r"chairman|chairperson",
            r"key managerial|\bkmp\b",
            r"senior management",
            r"auditor",
            r"company secretary",
            r"compliance officer",
        ],
        "high_if": [
            r"\bceo\b",
            r"chief executive",
            r"managing director",
            r"\bmd\b",
            r"chief financial",
            r"\bcfo\b",
            r"chairman|chairperson",
            r"whole[- ]time director",
            r"chief operating",
            r"\bcoo\b",
            r"resign\w*.{0,60}auditor",
        ],
        "low_if": [r"company secretary", r"compliance officer", r"cost auditor", r"secretarial auditor"],
        "reason": "Change in directors, key management or auditors",
    },
    {
        "id": "credit_rating",
        "category": "Credit Rating",
        "priority": "Medium",
        "patterns": [
            r"credit rating",
            r"rating (action|assigned|reaffirm\w*|upgrade\w*|downgrade\w*|revis\w*|withdraw\w*)",
            r"\bcrisil\b",
            r"\bicra\b",
            r"care ratings",
            r"india ratings",
            r"ind-ra",
            r"brickwork",
            r"acuit",
        ],
        "high_if": [r"downgrad", r"withdraw", r"default", r"negative"],
        "low_if": [r"reaffirm"],
        "reason": "Credit rating action",
    },
    {
        "id": "fundraising",
        "category": "Fundraising",
        "priority": "Medium",
        "patterns": [
            r"fund[- ]?raising",
            r"rais(e|ing) (of )?(capital|funds)",
            r"\bqip\b",
            r"qualified institutions? placement",
            r"non-?convertible debentures?",
            r"\bncds?\b",
            r"preferential (issue|allotment)",
            r"rights issue",
            r"\bfccbs?\b",
            r"convertible (bonds?|debentures?|warrants?)",
            r"\bipo\b",
            r"public issue",
            r"follow-?on",
            r"commercial paper",
            r"external commercial borrowing|\becb\b",
            r"private placement",
            r"allotment of (equity )?shares",
            r"allotment of (securities|debentures)",
        ],
        "high_if": [
            r"\bqip\b",
            r"qualified institutions?",
            r"rights issue",
            r"preferential",
            r"\bfccb",
            r"fund[- ]?raising",
            r"\bipo\b",
            r"follow-?on",
        ],
        "reason": "Capital raise or securities allotment",
    },
    {
        "id": "litigation_regulatory",
        "category": "Litigation & Regulatory",
        "priority": "Medium",
        "patterns": [
            r"litigation",
            r"lawsuit",
            r"\bcourt\b",
            r"tribunal",
            r"\bnclt\b",
            r"\bnclat\b",
            r"show[- ]cause",
            r"penalty",
            r"\bfine\b",
            r"search and seizure",
            r"enforcement directorate",
            r"income[- ]tax (department|authority)",
            r"\bgst\b.{0,30}(demand|notice|order)",
            r"demand (notice|order)",
            r"tax demand",
            r"assessment order",
            r"\bcbi\b",
            r"insolvency",
            r"winding[- ]up",
            r"arbitration",
            r"investigation",
            r"\bfraud",
            r"\bdefault",
            r"\bsebi\b.{0,60}\b(order|penalty|adjudicat\w*)",
        ],
        "high_if": [
            r"fraud",
            r"\bdefault",
            r"insolvency",
            r"winding[- ]up",
            r"search and seizure",
            r"enforcement directorate",
            r"\bcbi\b",
            r"show[- ]cause",
            r"supreme court",
            r"\bnclt\b",
            r"\bsebi\b.{0,60}\b(order|penalty|adjudicat\w*)",
        ],
        "reason": "Legal, tax or regulatory action",
    },
    {
        "id": "exchange_clarification",
        "category": "Business Update",
        "priority": "Medium",
        "patterns": [r"clarification", r"price movement", r"volume movement", r"news item|news report|media report"],
        "ambiguous": True,
        "reason": "Company response to an exchange query or media report; needs review",
    },
    {
        "id": "business_update",
        "category": "Business Update",
        "priority": "Medium",
        "patterns": [
            r"orders? (received|win|won|worth|bagged|secured)",
            r"bagged",
            r"letter of (award|intent)",
            r"\bloi\b",
            r"\bloa\b",
            r"\bcontract\b",
            r"capacity",
            r"\bcapex\b",
            r"capital expenditure",
            r"commission(ing)?",
            r"expansion",
            r"greenfield|brownfield",
            r"new plant|manufacturing (facility|plant)",
            r"launch",
            r"partnership",
            r"collaboration",
            r"\bmou\b",
            r"memorandum of understanding",
            r"tie-?up",
            r"alliance",
            r"(operational|business|trading|production|sales) update",
        ],
        "reason": "Operational or commercial development",
    },
    {
        "id": "analyst_meet",
        "category": "Routine Disclosure",
        "priority": "Low",
        "patterns": [
            r"analyst",
            r"investor meet",
            r"earnings call",
            r"conference call",
            r"investor presentation",
        ],
        "reason": "Analyst or investor meeting logistics",
    },
    # ---------- Generic filings whose content the title doesn't reveal ----------
    {
        "id": "press_release",
        "category": "Business Update",
        "priority": "Medium",
        "patterns": [r"press release|media release"],
        "ambiguous": True,
        "reason": "Press release; content not identifiable from the title",
    },
    {
        "id": "board_outcome",
        "category": "Business Update",
        "priority": "Medium",
        "patterns": [r"outcome of board meeting"],
        "ambiguous": True,
        "reason": "Board meeting outcome; decisions not identifiable from the title",
    },
]

# Unknown never means unimportant: unmatched filings are shown at Medium and flagged for review
FALLBACK = {
    "fin_category": "Other",
    "priority": "Medium",
    "rule_id": "fallback",
    "reason": "No rule matched; needs review",
    "needs_llm": 1,
}


def _join(patterns):
    """Combine a list of regexes into one case-insensitive 'any of these' regex."""
    if not patterns:
        return None
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


def _compile(rules: list) -> list:
    compiled = []
    for rule in rules:
        compiled.append(
            {
                "id": rule["id"],
                "category": rule["category"],
                "priority": rule["priority"],
                "reason": rule["reason"],
                "ambiguous": rule.get("ambiguous", False),
                "patterns": _join(rule["patterns"]),
                "also": _join(rule.get("also")),
                "exclude": _join(rule.get("exclude")),
                "high_if": _join(rule.get("high_if")),
                "low_if": _join(rule.get("low_if")),
            }
        )
    return compiled


_COMPILED = _compile(RULES)


def build_text(row: dict) -> str:
    parts = [row.get("category"), row.get("subcategory"), row.get("title"), row.get("detail")]
    text = " | ".join(str(p or "") for p in parts)
    return re.sub(r"\s+", " ", text).lower()


def classify(row: dict) -> dict:
    """Classify one announcement. Returns category, priority, rank, rule id, reason, needs_llm."""
    text = build_text(row)
    for rule in _COMPILED:
        if not rule["patterns"].search(text):
            continue
        if rule["also"] and not rule["also"].search(text):
            continue
        if rule["exclude"] and rule["exclude"].search(text):
            continue
        priority = rule["priority"]
        if rule["high_if"] and rule["high_if"].search(text):
            priority = "High"
        elif rule["low_if"] and rule["low_if"].search(text):
            priority = "Low"
        return {
            "fin_category": rule["category"],
            "priority": priority,
            "priority_rank": PRIORITY_RANK[priority],
            "rule_id": rule["id"],
            "reason": rule["reason"],
            "needs_llm": int(rule["ambiguous"]),
        }
    return {**FALLBACK, "priority_rank": PRIORITY_RANK[FALLBACK["priority"]]}


def classify_pending() -> int:
    """Classify every announcement that has no classification under the current rules."""
    pending = db.read_unclassified(RULES_VERSION)
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for item in pending:
        result = classify(item)
        rows.append(
            {"news_id": item["news_id"], **result, "rules_version": RULES_VERSION, "classified_at": now}
        )
    return db.upsert_classifications(rows)


def short_title(title: str) -> str:
    """BSE titles start with 'Company - 500325 - '. Keep only the informative tail."""
    parts = title.split(" - ", 2)
    return parts[-1] if len(parts) == 3 else title


def report(days: int = 30) -> None:
    """Print how well the rules cover the stored filings, to guide tuning."""
    classify_pending()
    df = get_announcements(days=days)
    if df.empty:
        print("No stored announcements. Run: python -m src.announcements")
        return
    total = len(df)
    print(f"Announcements stored for the last {days} days: {total}")
    print("\nBy priority:")
    print(df["priority"].value_counts().to_string())
    print("\nBy category:")
    print(df["fin_category"].value_counts().to_string())

    needs = df[df["needs_llm"] == 1]
    fallback = df[df["rule_id"] == "fallback"]
    print(f"\nFlagged for LLM review: {len(needs)} ({len(needs) / total:.0%})")
    print(f"Matched no rule at all: {len(fallback)} ({len(fallback) / total:.0%})")

    print("\n--- Latest 15 HIGH priority (check these are really important) ---")
    for _, r in df[df["priority"] == "High"].head(15).iterrows():
        print(f"{r['ticker']:<10} {r['filing_dt'][:10]} [{r['fin_category']}] {short_title(r['title'])[:95]}")

    print("\n--- 15 filings that matched no rule (candidates for new rules) ---")
    for _, r in fallback.head(15).iterrows():
        print(f"{r['ticker']:<10} {r['filing_dt'][:10]} ({r['category']}) {short_title(r['title'])[:95]}")


# (BSE category, subcategory, title, detail, expected category, expected priority)
TEST_CASES = [
    ("Result", "Financial Results", "Reliance Industries Ltd - 500325 - Announcement under Regulation 30 (LODR)-Financial Results for the quarter ended September 30, 2026", "", "Quarterly Results", "High"),
    ("Company Update", "", "Infosys Ltd - 500209 - Trading Window Closure for the quarter ended September 30, 2026", "", "Routine Disclosure", "Low"),
    ("Board Meeting", "", "TCS Ltd - 532540 - Intimation of Board Meeting to consider unaudited financial results", "", "Quarterly Results", "Medium"),
    ("Company Update", "", "Outcome of Board Meeting", "Further to our prior intimation, the Board approved unaudited financial results for the quarter ended September 30, 2026", "Quarterly Results", "High"),
    ("Company Update", "", "Resignation of Chief Financial Officer", "", "Management Change", "High"),
    ("Company Update", "", "Appointment of Chairman", "", "Management Change", "High"),
    ("Company Update", "", "Appointment of Company Secretary and Compliance Officer", "", "Management Change", "Low"),
    ("Company Update", "", "Allotment of NCDs", "", "Fundraising", "Medium"),
    ("Company Update", "", "Allotment of equity shares under ESOP", "", "Routine Disclosure", "Low"),
    ("Corp. Action", "", "Record date for interim dividend", "", "Corporate Action", "Medium"),
    ("Company Update", "", "Scheme of arrangement for demerger", "", "M&A / Restructuring", "High"),
    ("Company Update", "", "Newspaper publication of financial results", "", "Routine Disclosure", "Low"),
    ("Company Update", "", "Show cause notice received from SEBI", "", "Litigation & Regulatory", "High"),
    ("Company Update", "", "Credit rating reaffirmed by CRISIL", "", "Credit Rating", "Low"),
    ("Company Update", "", "Credit rating downgraded by ICRA", "", "Credit Rating", "High"),
    ("Company Update", "", "Order received worth Rs 5,000 crore", "", "Business Update", "Medium"),
    ("Company Update", "", "Disclosure under Regulation 29(2) of SEBI (SAST) Regulations", "", "Routine Disclosure", "Low"),
    ("Company Update", "", "Outcome of Board Meeting", "", "Business Update", "Medium"),
    ("Company Update", "", "Random text about nothing in particular", "", "Other", "Medium"),
]


def run_self_test() -> None:
    failures = 0
    for category, subcategory, title, detail, want_cat, want_pri in TEST_CASES:
        row = {"category": category, "subcategory": subcategory, "title": title, "detail": detail}
        got = classify(row)
        ok = got["fin_category"] == want_cat and got["priority"] == want_pri
        failures += not ok
        print(
            "PASS" if ok else "FAIL",
            f"want {want_cat}/{want_pri}",
            f"got {got['fin_category']}/{got['priority']} [{got['rule_id']}]",
            "|",
            title[:55],
        )
    generic = classify({"title": "Outcome of Board Meeting"})
    assert generic["needs_llm"] == 1, "generic board outcome should be flagged for LLM review"
    assert classify({"title": "xyz"})["priority"] == "Medium", "unknown filings must never be Low"
    assert failures == 0, f"{failures} rule test(s) failed"
    print("All rule tests passed.")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report()
    else:
        run_self_test()