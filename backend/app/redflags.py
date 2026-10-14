"""
app/redflags.py

Automated Red Flag & Governance Scanner.

Two-stage design, deliberately not "ask an LLM to read the whole filing and
list red flags":
  1. Cheap, deterministic RECALL pass -- keyword/regex rules pull every
     chunk that plausibly touches a governance-sensitive topic (related-party
     transactions, auditor qualification language, going-concern doubt,
     contingent liabilities, restatements, sudden management departures).
     This pass is intentionally over-inclusive.
  2. LLM PRECISION pass -- each candidate chunk is classified and summarized
     by the reasoning model, which assigns severity and discards false
     positives (e.g. a chunk that mentions "related party" only in a
     boilerplate definitions section).

Running the LLM only over the (small) candidate set from stage 1, rather
than the whole filing, keeps cost and latency bounded regardless of filing
length, and keeps the recall guarantee independent of the LLM's attention
span over a 200-page document.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

RULES: dict[str, list[re.Pattern]] = {
    "RELATED_PARTY_TRANSACTION": [
        re.compile(r"related[\s-]part(y|ies)", re.I),
        re.compile(r"transactions? with (our |the )?(directors|officers|promoters)", re.I),
    ],
    "AUDITOR_QUALIFICATION": [
        re.compile(r"qualified opinion", re.I),
        re.compile(r"except for", re.I),
        re.compile(r"material weakness(es)?", re.I),
        re.compile(r"emphasis of matter", re.I),
    ],
    "GOING_CONCERN": [
        re.compile(r"going concern", re.I),
        re.compile(r"substantial doubt", re.I),
    ],
    "CONTINGENT_LIABILITY": [
        re.compile(r"contingent liabilit(y|ies)", re.I),
        re.compile(r"pending litigation", re.I),
        re.compile(r"guarantees? (issued|provided)", re.I),
    ],
    "RESTATEMENT": [
        re.compile(r"restate(d|ment)", re.I),
        re.compile(r"correction of (an? )?error", re.I),
    ],
    "MANAGEMENT_CHANGE": [
        re.compile(r"resign(ation|ed)? of (the )?(chief|director|cfo|ceo)", re.I),
        re.compile(r"appointment of (a )?new (chief|director|cfo|ceo)", re.I),
    ],
}

SEVERITY_RANK = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

CLASSIFICATION_SYSTEM_PROMPT = """You are a forensic financial analyst reviewing candidate excerpts from a \
company's SEC/exchange filing that were flagged by keyword rules as POSSIBLY containing a governance red flag.

For the excerpt provided, respond with ONLY a JSON object (no markdown fences, no prose) of this exact shape:
{"is_red_flag": true|false, "category": "<one of RELATED_PARTY_TRANSACTION|AUDITOR_QUALIFICATION|GOING_CONCERN|\
CONTINGENT_LIABILITY|RESTATEMENT|MANAGEMENT_CHANGE|OTHER>", "severity": "<LOW|MEDIUM|HIGH|CRITICAL>", \
"summary": "<one sentence, specific to this excerpt, no boilerplate>"}

Set is_red_flag=false for boilerplate (definitions sections, standard risk-factor disclaimers, immaterial
disclosures) even if a keyword matched. Only set true for something a diligent analyst would actually flag."""


@dataclass
class Candidate:
    chunk_id: str
    filing_id: str
    text: str
    matched_categories: list[str]


def find_candidates(chunks: list[dict]) -> list[Candidate]:
    """Stage 1: cheap recall pass over already-retrieved/stored chunk text."""
    candidates = []
    for chunk in chunks:
        text = chunk.get("text", "")
        matched = [cat for cat, patterns in RULES.items() if any(p.search(text) for p in patterns)]
        if matched:
            candidates.append(Candidate(
                chunk_id=chunk["chunk_id"], filing_id=chunk["filing_id"],
                text=text, matched_categories=matched,
            ))
    return candidates


def classify_candidate(llm_client, candidate: Candidate, model: str | None = None) -> dict | None:
    """Stage 2: LLM precision pass on one candidate. Returns None if the
    model rejects it as not a genuine red flag."""
    model = model or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
    response = llm_client.models.generate_content(
        model=model,
        contents=f"Excerpt:\n\n{candidate.text[:3000]}",
        config={"system_instruction": CLASSIFICATION_SYSTEM_PROMPT, "max_output_tokens": 300},
    )
    raw = (response.text or "").strip()
    raw = re.sub(r"^```json|```$", "", raw).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not parsed.get("is_red_flag"):
        return None

    return {
        "chunk_id": candidate.chunk_id,
        "filing_id": candidate.filing_id,
        "category": parsed.get("category", "OTHER"),
        "severity": parsed.get("severity", "LOW") if parsed.get("severity") in SEVERITY_RANK else "LOW",
        "summary": parsed.get("summary", "").strip(),
        "evidence_text": candidate.text[:1000],
    }


def scan_chunks(llm_client, chunks: list[dict]) -> list[dict]:
    """Runs both stages over a list of already-retrieved chunk dicts and
    returns the confirmed red flags, sorted by severity (highest first)."""
    candidates = find_candidates(chunks)
    findings = []
    for candidate in candidates:
        result = classify_candidate(llm_client, candidate)
        if result:
            findings.append(result)
    findings.sort(key=lambda f: SEVERITY_RANK.index(f["severity"]), reverse=True)
    return findings
