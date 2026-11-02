from __future__ import annotations

# Canonical PF topic → accepted main_category / document_category substrings (incl. typos)
CANONICAL_TOPICS: dict[str, list[str]] = {
    "discipline": ["E&D", "Complaint", "Inquiry", "Fact Finding", "Fact Findinge", "Disciplinary"],
    "ed": ["E&D", "Fact Finding", "Fact Findinge"],
    "complaint": ["Complaint"],
    "inquiry": ["Inquiry", "Fact Finding", "Fact Findinge"],
    "leave": ["Leave"],
    "deputation": ["Deputation"],
    "posting": ["Transfer & Posting", "Transfer/Posting", "Transfer and Posting", "Posting"],
    "promotion": ["Promotion"],
    "training": ["Training", "Training & PDF", "Scholarship"],
    "noc": ["NOC"],
    "appointment": ["Appointment", "Offer of Appointment"],
    "charge": ["Charge Assumption", "Charge Relinguinsh", "Charge Relinquishment", "Additional Charge"],
    "suspension": ["Suspension"],
    "education": ["Educational Certificate", "Other - Educational Certificate", "Education"],
    "medical": ["Medical"],
    "family": ["Family"],
    "identity": ["CNIC", "Identity", "Domicile", "Personal Profile"],
    "retirement": ["Retirement", "LPR", "Pension"],
}


def category_match_sql(column: str, topics: list[str]) -> tuple[str, dict[str, str]]:
    patterns: list[str] = []
    for t in topics:
        patterns.extend(CANONICAL_TOPICS.get(t.lower(), [t]))
    if not patterns:
        return "1=1", {}
    clauses = []
    params: dict[str, str] = {}
    for i, p in enumerate(patterns):
        key = f"pat{i}"
        clauses.append(f"LOWER(ISNULL({column},'')) LIKE :{key}")
        params[key] = f"%{p.lower()}%"
    return "(" + " OR ".join(clauses) + ")", params
