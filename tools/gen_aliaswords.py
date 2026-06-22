"""
One-time generator that froze shared/alias.py's ADJECTIVES + NOUNS wordlists.

Provenance / reproducibility only — DO NOT re-run to "refresh" the lists. The lists are a frozen
contract (see shared/alias.py): regenerating against an updated upstream would silently rename
every existing host and break reputation continuity. This file documents exactly how the shipped
lists were derived, so the choice is auditable, not so it can be re-rolled.

Sources (fetched once, 2026-06):
  - POS tags: Atkinson/SCOWL "part-of-speech.txt"
    https://raw.githubusercontent.com/en-wl/wordlist/master/pos/part-of-speech.txt
  - Word frequency (commonness): hermitdave FrequencyWords en_50k.txt
    https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/en/en_50k.txt
  - Profanity filter: LDNOOBW English list
    https://raw.githubusercontent.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words/master/en

Method: take words tagged as a PURE adjective ('A') or PURE noun ('N') — pure tags make the two
lists automatically disjoint — keep lowercase ASCII length 4-8, drop the profanity list, a manual
blocklist, function-word stopwords, and any word with an offensive substring. Then keep only words
that appear in the frequency list (so every alias word is COMMON/recognizable) and take the 1024
most frequent of each, stored sorted alphabetically (deterministic).

Usage (paths default to /tmp downloads):
  python tools/gen_aliaswords.py POS_FILE FREQ_FILE BADWORDS_FILE > shared/alias.py
"""
from __future__ import annotations

import re
import sys

TARGET = 1024
MINLEN, MAXLEN = 4, 8

# Extra exclusions beyond the LDNOOBW list — words that are clean by the dictionary but read badly
# in an auto-generated public name (violence/death/disease/drugs/bodily/derogatory/sensitive).
MANUAL_BLOCK = {
    "dead", "dying", "death", "kill", "killed", "corpse", "morgue", "coffin", "grave", "tomb",
    "blood", "bloody", "wound", "gore", "knife", "gun", "rifle", "pistol", "bomb", "ammo",
    "slave", "slaves", "racist", "racism", "rape", "raped", "nazi", "hitler", "jihad", "terror",
    "cancer", "tumor", "tumour", "plague", "leper", "rabies", "syphilis", "herpes", "ulcer",
    "vomit", "feces", "faeces", "urine", "snot", "phlegm", "mucus", "pus", "scab", "boil",
    "drug", "drugs", "heroin", "opium", "crack", "meth", "addict", "junkie", "drunk", "stoned",
    "idiot", "moron", "stupid", "dumb", "loser", "ugly", "freak", "creep", "savage", "brute",
    "demon", "satan", "devil", "hell", "damned", "curse", "cursed", "witch", "voodoo",
    "naked", "nude", "groin", "thigh", "buttock", "nipple", "loins",
    "fascist", "commie", "infidel", "heathen", "pagan",
    # social / dark / sensitive topics that read badly as a friendly name
    "suicide", "abortion", "hatred", "hostage", "victim", "refugee", "funeral", "widow",
    "orphan", "prison", "prisoner", "tyrant", "traitor", "captive", "torture", "misery",
    "poverty", "famine", "drought", "disaster", "tragedy", "murder", "murderer", "assault",
    "robbery", "scandal", "betrayal", "coffins", "warfare", "genocide", "massacre", "hostile",
    "morbid", "lethal", "deadly", "fatal", "violent", "brutal", "vicious", "evil", "wicked",
    # additions found in a full review of the generated lists (slurs/sexual/drugs/weapons/
    # death/disease/hard-religious/proper-nouns/strongly-negative) — see tools comment.
    "homo", "guinea", "ghetto", "retarded", "bitchy", "jackass", "weirdo", "swine", "wretch",
    "moronic", "idiotic", "obese", "senile", "demented", "deranged", "depraved", "perverse",
    "sadistic", "pissed", "frigging", "lewd", "obscene", "vulgar", "carnal", "pubic", "virile",
    "sexier", "sexiest", "brothel", "bikini", "booty", "condom", "sperm", "womb", "mistress",
    "alcohol", "beer", "bourbon", "vodka", "whiskey", "whisky", "martini", "tequila", "cocaine",
    "morphine", "ecstasy", "tobacco", "cigar", "assassin", "sniper", "bomber", "bullet",
    "grenade", "missile", "gunfire", "gunshot", "weapon", "knives", "homicide", "treason",
    "slavery", "predator", "felony", "theft", "burglar", "burglary", "robber", "culprit",
    "crime", "cult", "cartel", "gangster", "thug", "cemetery", "burial", "coroner", "disease",
    "illness", "insanity", "madness", "madman", "trauma", "seizure", "syndrome", "diabetes",
    "asthma", "agony", "sadness", "remorse", "grief", "cruelty", "perished", "mosque", "convent",
    "altar", "prophet", "prophecy", "pastor", "preacher", "vicar", "buddha", "lucifer",
    "holiness", "satanic", "demonic", "unholy", "godless", "autistic", "bipolar", "comatose",
    "diseased", "pregnant", "unborn", "fetal", "suicidal", "suicide", "lifeless", "immoral",
    "indecent", "racial", "abusive", "addicted", "illegal", "illicit", "heinous", "ruthless",
    "cruel", "vengeful", "spiteful", "insane", "prius", "amazon", "boston", "brazil", "texas",
    "liang", "kang", "yuan", "batman", "superman", "tellin", "takin", "baba", "amigo", "senor",
    "damning", "gunned", "killer", "hateful", "sinner", "sinful", "sickness", "warsaw",
}
# Offensive substrings — catch compounds the whole-word lists miss.
BAD_SUBSTR = ("fuck", "shit", "cunt", "nigg", "fag", "rape", "whore", "slut", "semen",
              "penis", "vagina", "boob", "porn", "puss", "dick", "cock", "tit")

# Function words that SCOWL sometimes tags N/A but which read terribly as a name ("just-cat").
STOPWORDS = {
    "this", "that", "these", "those", "here", "there", "then", "than", "thus", "such",
    "just", "only", "even", "still", "more", "most", "much", "many", "less", "least",
    "down", "over", "under", "into", "onto", "upon", "with", "from", "about", "above",
    "other", "another", "same", "each", "every", "either", "neither", "some", "none",
    "what", "when", "where", "which", "while", "whom", "whose", "will", "would", "shall",
    "should", "could", "might", "must", "your", "yours", "their", "them", "they", "ours",
    "mine", "hers", "him", "her", "his", "its", "our", "you", "she", "him", "who", "why",
    "how", "yes", "not", "nor", "but", "and", "for", "the", "are", "was", "were", "been",
    "being", "have", "had", "has", "did", "does", "done", "able", "very", "too", "also",
    "ever", "never", "always", "again", "once", "soon", "yet", "etc", "via",
}


def candidates(pos_path: str, bad_path: str, names_paths: list[str]):
    bad = {w.strip().lower() for w in open(bad_path, encoding="utf-8") if w.strip()}
    bad |= MANUAL_BLOCK | STOPWORDS
    for np in names_paths:                     # drop person/place names that slip through lowercased
        bad |= {w.strip().lower() for w in open(np, encoding="utf-8") if w.strip()}
    adj, noun = set(), set()
    for line in open(pos_path, encoding="latin-1"):
        if "\t" not in line:
            continue
        word, code = line.rstrip("\n").split("\t", 1)
        w = word  # do NOT lowercase: matching the raw word against [a-z] drops proper nouns
        if not re.fullmatch(rf"[a-z]{{{MINLEN},{MAXLEN}}}", w):
            continue
        if w in bad or any(s in w for s in BAD_SUBSTR):
            continue
        if code == "A":
            adj.add(w)
        elif code == "N":
            noun.add(w)
    overlap = adj & noun                       # belt-and-suspenders: keep the two lists disjoint
    adj -= overlap
    noun -= overlap
    return adj, noun


def freq_order(freq_path: str) -> list:
    """Words from most to least frequent (dedup, first occurrence wins)."""
    seen, order = set(), []
    for line in open(freq_path, encoding="utf-8"):
        parts = line.split()
        if parts and parts[0].lower() not in seen:
            seen.add(parts[0].lower())
            order.append(parts[0].lower())
    return order


def pick(words: set, order: list) -> list:
    """The 1024 most COMMON candidates (by frequency rank), stored sorted alphabetically."""
    common = [w for w in order if w in words]
    if len(common) < TARGET:
        raise SystemExit(f"only {len(common)} common candidates; need {TARGET}")
    return sorted(common[:TARGET])


def fmt(name: str, words: list) -> str:
    lines = [f"{name} = ("]
    for i in range(0, len(words), 8):
        row = ", ".join(f'"{w}"' for w in words[i:i + 8])
        lines.append(f"    {row},")
    lines.append(")")
    return "\n".join(lines)


HEADER = '''"""
Host aliases — deterministic, unforgeable two-word names derived from a host's pubkey.

A host can't choose its alias (it falls out of its key), and there's no registrar: host and client
each compute the SAME name independently and offline. The client ALWAYS derives from the
signature-verified pubkey and ignores any alias a listing might carry, so a host can't stuff a
name in to impersonate another. Always render with the pubkey tail: "eloquent-cat · 2a2f".

Single source of truth: both host/ and client/ import derive_alias + the lists from HERE. Never
copy them — divergence would make the two sides derive different names and break verification.

THE WORDLISTS ARE A FROZEN CONTRACT. 1024 adjectives + 1024 nouns, generated once (see
tools/gen_aliaswords.py) and reviewed. Reordering or editing any entry silently renames every
existing host and breaks reputation continuity — treat this like a locked migration: don't touch it.
"""
from __future__ import annotations

import hashlib


def derive_alias(pubkey_hex: str) -> str:
    """Map a host pubkey to its two-word alias, e.g. "eloquent-cat".

    Pinned so host and client agree byte-for-byte: SHA-256 over the RAW 32 pubkey bytes (not the
    hex string), first 4 bytes big-endian -> two 10-bit indices (adjective from bits 10-19, noun
    from bits 0-9). Non-hex/odd identities (dev/mock keys) fall back to hashing the string bytes —
    both sides still agree because they run this same function.
    """
    try:
        raw = bytes.fromhex(pubkey_hex)
    except ValueError:
        raw = pubkey_hex.encode()
    n = int.from_bytes(hashlib.sha256(raw).digest()[:4], "big")
    return f"{ADJECTIVES[(n >> 10) & 0x3FF]}-{NOUNS[n & 0x3FF]}"


def alias_label(pubkey_hex: str) -> str:
    """The canonical display form: "<alias> · <first 4 hex of pubkey>". The tail disambiguates
    the ~1-in-1M word collisions; the pubkey remains the only thing that decides identity."""
    return f"{derive_alias(pubkey_hex)} · {pubkey_hex[:4]}"

'''


def main():
    pos_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/part-of-speech.txt"
    freq_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/en50k.txt"
    bad_path = sys.argv[3] if len(sys.argv) > 3 else "/tmp/badwords.txt"
    names_paths = sys.argv[4:] or ["/tmp/firstnames.txt", "/tmp/surnames.txt"]
    adj, noun = candidates(pos_path, bad_path, names_paths)
    order = freq_order(freq_path)
    adjs, nouns = pick(adj, order), pick(noun, order)
    assert not (set(adjs) & set(nouns)), "lists must be disjoint"
    out = HEADER + "\n" + fmt("ADJECTIVES", adjs) + "\n\n" + fmt("NOUNS", nouns) + "\n"
    sys.stdout.write(out)


if __name__ == "__main__":
    main()
