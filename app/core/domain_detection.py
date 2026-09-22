from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import numpy as np

# Multilingual model — understands English, Roman Urdu, and other languages
# in the same embedding space, so paraphrased or mixed-language messages
# still match correctly.
_model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')

# Reference phrases per domain. Not exhaustive — grows over time as real
# usage and eval failures reveal new phrasings.
DOMAIN_REFERENCES = {
    "grief": [
        "I lost someone I loved",
        "They passed away and I miss them",
        "I feel empty since they're gone",
        "It's been months but I still cry about them",
        "mujhe uski yaad aati hai",
        "woh guzar gaye",
        "unki kami mehsoos hoti hai",
    ],
    "trauma": [
        "I keep having flashbacks",
        "I can't stop thinking about what happened to me",
        "I freeze when something reminds me of it",
        "I still have nightmares about that day",
        "mujhe woh waqia bhool nahi raha",
        "us waqt ke baad se dar lagta hai",
    ],
    "anxiety": [
        "I'm worried something bad will happen",
        "I can't stop thinking what if",
        "My heart races and I can't calm down",
        "I keep imagining the worst outcome",
        "mujhe bohot pareshani ho rahi hai",
        "dil ghabra raha hai",
    ],
    "depression": [
        "I have no energy to do anything",
        "Nothing feels worth doing anymore",
        "I feel heavy and empty all the time",
        "I can't get myself out of bed",
        "kuch karne ka dil nahi karta",
        "bohot thakan si rehti hai",
    ],
    "relationships": [
        "We had another argument",
        "I feel distant from my family",
        "My friend hurt me and I don't know what to say",
        "I feel like nobody understands me",
        "ghar walon se baat nahi ho rahi",
        "uske saath rishta kharab ho raha hai",
    ],
    "positive": [
        "Today was a really good day",
        "I'm proud of what I did",
        "Something nice happened and I want to remember it",
        "I felt genuinely happy today",
        "aaj bohot acha din tha",
        "mujhe khushi hui aaj",
    ],
}

# Lower number = higher priority when several domains are active.
# Safety-sensitive domains come first.
DOMAIN_PRIORITY = {
    "trauma": 1,
    "grief": 2,
    "depression": 3,
    "anxiety": 4,
    "relationships": 5,
    "positive": 6,
    "general": 7,
}

# Maps onboarding focus area codes to detection domains, since the two
# vocabularies differ (onboarding uses clinical-ish labels, domains do not).
FOCUS_AREA_TO_DOMAIN = {
    "anxiety": "anxiety",
    "depression": "depression",
    "trauma_ptsd": "trauma",
    "ocd": "anxiety",
    "adhd": "general",
    "addiction": "general",
    "not_sure": "general",
    "other": "general",
}

DOMAIN_THRESHOLD = 0.45
ONBOARDING_BOOST = 0.05
MAX_DOMAINS = 2

# Pre-compute reference embeddings once at import time (not per-request)
_reference_embeddings = {
    domain: _model.encode(phrases)
    for domain, phrases in DOMAIN_REFERENCES.items()
}


def score_message_semantic(text: str) -> dict[str, float]:
    """
    Scores a message against each domain's reference phrases using
    semantic similarity (not exact keyword matching). Returns the
    highest similarity score per domain.
    """
    text_embedding = _model.encode([text])
    scores = {}
    for domain, ref_embeddings in _reference_embeddings.items():
        similarities = cosine_similarity(text_embedding, ref_embeddings)[0]
        scores[domain] = float(np.max(similarities))
    return scores


def select_domains(text: str, account_focus_areas: list[str] | None = None) -> list[str]:
    """
    Determines which domain(s) apply to this message. Runs fresh on
    every message — never relies solely on onboarding focus areas,
    since what a user is going through can change day to day.
    """
    account_focus_areas = account_focus_areas or []
    scores = score_message_semantic(text)

    # Translate onboarding codes into domain names before boosting.
    boosted = {
        FOCUS_AREA_TO_DOMAIN.get(code)
        for code in account_focus_areas
    }

    # Onboarding acts only as a small tie-breaker, never a source of truth.
    for domain in boosted:
        if domain in scores:
            scores[domain] += ONBOARDING_BOOST

    active = {d: s for d, s in scores.items() if s >= DOMAIN_THRESHOLD}
    sorted_domains = sorted(active.keys(), key=lambda d: DOMAIN_PRIORITY.get(d, 99))

    if not sorted_domains:
        fallback = [d for d in boosted if d and d != "general"]
        sorted_domains = fallback[:1] or ["general"]

    return sorted_domains[:MAX_DOMAINS]