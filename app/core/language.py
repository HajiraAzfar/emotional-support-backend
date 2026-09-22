"""
Which language the user is writing in, decided in code so the reply language
does not depend on the model noticing it. Heuristic, not a classifier: Roman
Urdu is recognised by its common function words.
"""
import re

ENGLISH, ROMAN_URDU, MIXED, URDU_SCRIPT = "english", "roman_urdu", "mixed", "urdu_script"

# Frequent Roman Urdu words that are not English words (so "main", "to", "me" are left out).
_ROMAN_URDU_WORDS = {
    "hai", "hain", "hay", "tha", "thi", "thay", "ho", "hoon", "hun", "hua", "hui", "hue",
    "nahi", "nahin", "nai", "na", "mein", "mai", "mujhe", "mujh", "mera", "meri", "mere", "hum", "humein",
    "ko", "ka", "ki", "ke", "se", "par", "pe", "bhi", "sirf", "aur", "ya", "lekin", "magar", "toh",
    "kya", "kyun", "kyon", "kaise", "kab", "kahan", "kaun", "kitna", "kuch", "sab", "bohot", "bahut",
    "acha", "achha", "accha", "theek", "thik", "bura", "yeh", "ye", "woh", "wo", "aap", "tum", "unhon",
    "isliye", "shayad", "abhi", "aaj", "kal", "raha", "rahi", "rahe", "kar", "karna", "karti", "karta",
    "kiya", "gaya", "gayi", "diya", "dil", "zindagi", "pata", "lagta", "lagti", "laga", "chahiye",
    "sakta", "sakti", "wala", "wali", "jab", "agar", "phir", "yaar", "ammi", "abbu", "ghar",
}
_WORD = re.compile(r"[a-zA-Z']+")
_URDU_CHARS = re.compile(r"[؀-ۿ]")
MIN_WORDS = 3  # shorter messages ("ok", "idk") say nothing about language


def detect(text: str) -> str | None:
    """Language of one message, or None when it is too short to tell."""
    if not text:
        return None
    if len(_URDU_CHARS.findall(text)) >= 3:
        return URDU_SCRIPT
    words = [w.lower() for w in _WORD.findall(text)]
    if len(words) < MIN_WORDS:
        return None
    ratio = sum(w in _ROMAN_URDU_WORDS for w in words) / len(words)
    if ratio >= 0.3:
        return ROMAN_URDU
    if ratio >= 0.12:
        return MIXED
    return ENGLISH


def reply_language(user_texts: list[str], default: str = ENGLISH) -> str:
    """Language of the most recent user text that is long enough to tell."""
    for text in reversed(user_texts):
        found = detect(text)
        if found:
            return found
    return default


_AAP = (
    " Always address the user as \"aap\" (aap, aapka, aapki, aapko, aapne, aap se) with the matching "
    "respectful verb forms (\"aap kar sakti hain\", \"aapko kaisa laga\") — never \"tum\" or \"tu\", "
    "even if the user writes that way."
)

_INSTRUCTIONS = {
    ENGLISH: "The user is writing in English. Write your whole reply in English.",
    ROMAN_URDU: (
        "The user is writing in Roman Urdu (Urdu in Latin letters). Write your whole reply in "
        "Roman Urdu, the way they write — not in English and not in Urdu script. Common English "
        "words people mix in (like 'office', 'meeting', 'stress') are fine." + _AAP
    ),
    MIXED: (
        "The user mixes Roman Urdu and English. Reply in the same natural mix, leaning on Roman "
        "Urdu for the feeling words — not in pure English." + _AAP
    ),
    URDU_SCRIPT: (
        "The user is writing in Urdu script. Write your whole reply in Urdu script. Always address "
        "the user as آپ, never تم."
    ),
}

# Informal second person, which Echo must never use (checked by the eval set).
TUM_WORDS = re.compile(r"\b(tum|tumhara|tumhari|tumhare|tumhe|tumhein|tumko|tumse|tumne|tujhe|tera|teri|tere)\b", re.I)


def instruction(language: str) -> str:
    return _INSTRUCTIONS.get(language, _INSTRUCTIONS[ENGLISH])
