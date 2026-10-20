"""Turn natural-language messages into memory-search queries.

Raw user text can't go straight into an FTS5 ``MATCH``: punctuation like
``?``, ``'`` or ``:`` is query syntax (→ OperationalError), and bare words are
AND-ed, so "how's maya doing at the bakery?" only matches a stored message
containing *every* word. Instead we pull out the meaningful terms, quote them
(so they're always literal) and OR them together; bm25 then ranks messages
that share more / rarer terms higher.

Pure Python, no dependencies — runs the same on Termux.
"""

from __future__ import annotations

import re

# Small, conversational stopword list: words that carry no recall signal in
# chat ("how's it going", "what do you think about…"). Deliberately keeps
# names, nouns, and most verbs.
STOPWORDS: frozenset[str] = frozenset(
    """
    a about above after again against all also am an and any are aren as at
    be because been before being below between both but by can cant could
    couldnt did didnt do does doesnt doing dont down during each few for from
    further get gets getting go goes going gonna got had hadnt has hasnt have
    havent having he hed hes her here heres hers herself him himself his how
    hows i id ill im ive if in into is isnt it its itself just know lets like
    me more most much my myself no nor not now of off oh ok okay on once only
    or other ought our ours ourselves out over own really same say said she
    shes should shouldnt so some such than that thats the their theirs them
    themselves then there theres these they theyd theyll theyre theyve thing
    things think this those through to too u under until up ur us very want
    was wasnt we wed well were weve werent what whats when where wheres which
    while who whos whom why will with wont would wouldnt ya yeah yes yet you
    youd youll youre youve your yours yourself yourselves hey hi hello lol
    lmao omg pls please thanks thank tell remember recall
    """.split()
)

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)
MAX_TERMS = 12


def keywords(text: str, *, max_terms: int = MAX_TERMS) -> list[str]:
    """Meaningful, de-duplicated lowercase terms from ``text`` (in order)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _WORD_RE.findall(text.lower()):
        word = raw.strip("'_")
        if word.endswith("'s"):
            word = word[:-2]
        word = word.replace("'", "")
        if not word or word in STOPWORDS:
            continue
        if len(word) < 2 and not word.isdigit():
            continue
        if word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= max_terms:
            break
    return out


_SUFFIXES = ("ies", "ing", "ed", "es", "s", "y")


def stem(term: str) -> str:
    """Crude suffix strip so a prefix match covers word forms both ways:
    bakery/bakeries → "baker", started/starting → "start". Never shorter
    than 4 letters, so names like "maya" are left alone."""
    for suf in _SUFFIXES:
        if term.endswith(suf) and len(term) - len(suf) >= 4:
            return term[: -len(suf)]
    return term


def fts_query(terms: list[str]) -> str:
    """OR-query of quoted terms; longer terms prefix-match on their stem."""
    parts: list[str] = []
    for t in terms:
        t = t.replace('"', "")
        if len(t) >= 4:
            parts.append('"' + stem(t) + '"*')
        else:
            parts.append('"' + t + '"')
    return " OR ".join(parts)


def term_score(content: str, terms: list[str]) -> int:
    """How many distinct terms appear in ``content`` (LIKE-fallback ranking)."""
    low = content.lower()
    return sum(1 for t in terms if t in low)
