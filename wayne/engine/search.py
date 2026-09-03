"""
Web search functionality for Alfred AI.
"""
import concurrent.futures
import warnings

warnings.filterwarnings("ignore")

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

_SEARCH_TIMEOUT = 8  # seconds before giving up


def google_search(query, num_results=3):
    def _run():
        results = []
        with DDGS() as ddgs:
            for result in ddgs.text(query, max_results=num_results):
                try:
                    results.append({
                        'url': result.get('href', ''),
                        'title': result.get('title', ''),
                        'snippet': result.get('body', '')
                    })
                    if len(results) >= num_results:
                        break
                except Exception:
                    continue
        return results

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run)
            return future.result(timeout=_SEARCH_TIMEOUT)
    except concurrent.futures.TimeoutError:
        print(f"\033[90m[Search timed out after {_SEARCH_TIMEOUT}s]\033[0m")
        return []
    except Exception as e:
        print(f"\033[90m[Search error: {str(e)}]\033[0m")
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
    'how do you feel', 'your view', 'your take', 'thoughts on',
    'what would you do', 'should i', 'do you reckon', 'agree',
]


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
