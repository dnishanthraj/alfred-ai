"""
Web search.

Two providers, chosen by whether a key is configured:

  DuckDuckGo (default) needs no key and no account, which is the right default
  for something you clone and run. It is scraped rather than an API, so it is
  rate-limited under repeated use and occasionally returns nothing at all.

  Brave (when BRAVE_API_KEY is set) is a real search API with a free tier. It
  returns cleaner snippets and does not fall over when queried several times in
  a minute, which is exactly when the default disappoints.

Neither is best in the abstract: the default is best for a stranger cloning the
repo, the alternative is best once you use it daily.
"""
import concurrent.futures
import os
import re
import warnings

import requests

warnings.filterwarnings("ignore")

try:
    from ddgs import DDGS
except ImportError:  # older releases of the same package
    from duckduckgo_search import DDGS

_SEARCH_TIMEOUT = 8  # seconds before giving up
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")


def provider_name():
    return "brave" if BRAVE_API_KEY else "duckduckgo"


def _search_brave(query, num_results):
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": num_results},
        headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
        timeout=_SEARCH_TIMEOUT,
    )
    response.raise_for_status()
    results = response.json().get("web", {}).get("results", [])
    return [
        {
            "url": r.get("url", ""),
            "title": r.get("title", ""),
            "snippet": r.get("description", ""),
        }
        for r in results[:num_results]
    ]


def _search_duckduckgo(query, num_results):
    results = []
    with DDGS() as ddgs:
        for result in ddgs.text(query, max_results=num_results):
            results.append({
                "url": result.get("href", ""),
                "title": result.get("title", ""),
                "snippet": result.get("body", ""),
            })
            if len(results) >= num_results:
                break
    return results


def google_search(query, num_results=3):
    """
    Look something up. Returns [] on any failure: a failed search should leave
    the contact to answer from what it knows and say so, not derail the turn
    with an exception.
    """
    backend = _search_brave if BRAVE_API_KEY else _search_duckduckgo
    try:
        # Both providers block, and occasionally hang past their own timeouts,
        # so they run behind a hard deadline of ours.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(backend, query, num_results).result(
                timeout=_SEARCH_TIMEOUT + 2)
    except Exception:
        return []


# Explicit search verbs. ONLY these (or a clear factual-lookup phrase) trigger
# a web search. This is the single source of truth — opinion/feeling/chat never
# routes to search unless one of these is present.
_SEARCH_VERBS = [
    'search', 'look up', 'look it up', 'look into', 'google it', 'google',
    'find out', 'find info', 'dig up', 'dig into', 'pull up',
    'latest news', 'the news', 'what happened to', 'what happened in',
    'price of', 'weather', 'who won', 'release date', 'when does', 'when is the',
    'how much is', 'how much does', 'search for', 'can you find',
    'anything you can find', 'what can you find',
]

# Phrases that signal an OPINION or PERSONAL exchange — never a web search,
# even if Alfred just asked an open question. Opinions are Alfred's job, not Google's.
_OPINION_MARKERS = [
    'your opinion', 'opinion on', 'what do you think', 'do you think',
    'what do you make of', 'what did you make of', 'how do you rate',
    'how do you feel', 'your view', 'your take', 'thoughts on',
    'what would you do', 'should i', 'do you reckon', 'agree',
]


# Questions that plainly need a fact nobody in a kitchen would have. These are
# searched *before* the model is asked anything.
#
# Letting the character decide was the better design and it does not survive
# contact with this character: the persona insists, at length and in his own
# voice, that he is an old man with a cup of tea and no computer. That is Alfred
# — and asked to look something up he would rather say he has not heard of it,
# or worse, guess. The lookup therefore happens on the way in, and he is handed
# what was found. He still gets to refuse, disbelieve it, or tell the operator
# to do his own homework.
FACTUAL_PATTERNS = [
    r"^\s*(who|what|when|where)\s+(is|are|was|were|does|did|do)\b",
    r"\bwho'?s\b.*\?",
    r"\bweather\b", r"\bforecast\b", r"\btemperature\b",
    r"\bnews\b", r"\bheadlines\b",
    r"\bprice of\b", r"\bhow much (is|does|are)\b",
    r"\bwho won\b", r"\bwhat happened (to|in|with)\b",
    r"\brelease date\b", r"\bfixtures?\b", r"\bscore\b",
    r"\blook (it |this |that )?up\b", r"\bsearch (for|up)\b",
    r"\bfind out\b", r"\bwhat do we have on\b", r"\btell me about\b",
]


# Questions whose subject is one of the two people on the line. They match the
# factual patterns perfectly — "Who are you?" is `who` + `are` — and searching
# them is both useless and absurd: the web does not know who he is, and asking it
# produced a lookup for a butler while he stood there being one. Whatever the
# answer is, it is in the persona or the vault, never online.
_PERSONAL_SUBJECTS = [
    r"\bwho\s+(are|were)\s+you\b",
    r"\bwho\s+(am|was)\s+i\b",
    r"\bwhat\s+(are|were)\s+you\b",
    r"\bwhat\s+(is|was|are|were)\s+(my|your|our)\b",
    r"\bwhat\s+do\s+(you|i|we)\b",
    r"\bwhere\s+(are|were)\s+(you|we)\b",
    r"\bhow\s+are\s+you\b",
]


def is_factual_lookup(prompt):
    """
    True when a question plainly needs a current fact.

    Deliberately conservative — two vetoes come before the patterns. An opinion
    marker vetoes it, because "what do you think of X" is not a lookup however
    factual X sounds; and a question *about* either person on the line vetoes it,
    because that answer never lived on the web.
    """
    lowered = (prompt or "").lower().strip()
    if any(marker in lowered for marker in _OPINION_MARKERS):
        return False
    if any(re.search(subject, lowered) for subject in _PERSONAL_SUBJECTS):
        return False
    return any(re.search(pattern, lowered) for pattern in FACTUAL_PATTERNS)


def needs_search(prompt, last_alfred_msg=""):
    prompt_lower = prompt.lower().strip()

    # HARD BLOCK: opinion / personal questions never go to search.
    # This overrides everything below, including Alfred-just-asked logic.
    if any(m in prompt_lower for m in _OPINION_MARKERS):
        return False

    # Only treat "Alfred just asked" as a search cue if Alfred was clearly
    # asking WHAT TO LOOK UP — not just making open conversation.
    if last_alfred_msg:
        alfred_seeking_search = [
            "what should i search", "what should i look up",
            "what do you want me to find", "what would you like me to look",
            "what shall i look into", "what am i searching",
        ]
        if any(p in last_alfred_msg.lower() for p in alfred_seeking_search):
            return True

    # Otherwise: only search when the user uses an explicit search verb.
    if any(verb in prompt_lower for verb in _SEARCH_VERBS):
        return True

    return False


def format_search_results(results):
    if not results:
        return ""

    formatted = "\n[Search Results]:\n"
    for i, result in enumerate(results, 1):
        formatted += f"{i}. {result.get('title', 'No title')}\n"
        if result.get('snippet'):
            formatted += f"   {result['snippet']}\n"
        formatted += f"   {result.get('url', '')}\n\n"

    return formatted
