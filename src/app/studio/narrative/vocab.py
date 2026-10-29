"""Controlled vocabulary shared by the structure pass, the Director and the delivery layer."""
from __future__ import annotations

import re

EMOTIONS = (
    "neutral", "calm", "joy", "excitement", "tenderness", "sadness", "grief", "fear", "anxiety",
    "anger", "surprise", "disgust", "determination", "humor", "resignation",
)
PACINGS = ("slow", "measured", "normal", "brisk", "fast")
INTENSITY_MIN, INTENSITY_MAX = 1, 5

# How each emotion is phrased for a TTS instruction (kept short and concrete).
EMOTION_PHRASES = {
    "neutral": "in a neutral, natural narrative tone",
    "calm": "calmly and evenly",
    "joy": "warmly, with audible joy",
    "excitement": "with eager excitement",
    "tenderness": "tenderly and gently",
    "sadness": "with sadness",
    "grief": "with deep grief, voice heavy",
    "fear": "fearfully, breath tight",
    "anxiety": "anxiously, slightly unsteady",
    "anger": "angrily, with edge",
    "surprise": "with surprise",
    "disgust": "with disgust",
    "determination": "with firm determination",
    "humor": "with dry, light humor",
    "resignation": "with weary resignation",
}
INTENSITY_ADVERB = {1: "very slightly", 2: "slightly", 3: "moderately", 4: "strongly", 5: "intensely"}
PACING_PHRASES = {"slow": "slow pace", "measured": "measured pace", "normal": "natural pace", "brisk": "brisk pace", "fast": "fast pace"}

# Related emotions: a move inside a family is a smooth shift, a move across families is a jump.
FAMILIES = {
    "neutral": "flat", "calm": "flat", "resignation": "low", "sadness": "low", "grief": "low", "tenderness": "warm", "joy": "warm",
    "humor": "warm", "excitement": "high", "surprise": "high", "determination": "high", "anger": "hot", "disgust": "hot",
    "fear": "tense", "anxiety": "tense",
}

SPEECH_VERBS = (
    "said|asked|replied|answered|whispered|shouted|yelled|cried|called|murmured|muttered|snapped|sighed|laughed|exclaimed|demanded|"
    "insisted|added|continued|began|suggested|offered|warned|breathed|growled|hissed|snarled|sobbed|stammered|stuttered|pleaded|begged|"
    "announced|declared|remarked|observed|explained|objected|protested|groaned|gasped|scoffed|sneered|barked|thundered|bellowed|"
    "screamed|shrieked|roared|whimpered|croaked|rasped|chuckled|interrupted|agreed|admitted|conceded|stated|noted|mused|wondered|"
    "ventured|prompted|urged|cautioned|retorted|countered|sniffed|panted|wailed"
)

TITLES = ("mr", "mrs", "ms", "miss", "dr", "prof", "professor", "captain", "capt", "sir", "lady", "lord", "madam", "madame", "mister",
          "detective", "sergeant", "sgt", "officer", "father", "mother", "uncle", "aunt", "king", "queen", "prince", "princess", "judge")
TITLE_GENDER = {"mr": "male", "mrs": "female", "ms": "female", "miss": "female", "sir": "male", "lady": "female", "lord": "male",
                "madam": "female", "madame": "female", "mister": "male", "father": "male", "mother": "female", "uncle": "male",
                "aunt": "female", "king": "male", "queen": "female", "prince": "male", "princess": "female"}

NOT_NAMES = {
    "the", "a", "an", "then", "and", "but", "or", "so", "yet", "when", "as", "if", "it", "there", "this", "that", "these", "those", "his",
    "her", "their", "my", "our", "your", "its", "after", "before", "while", "once", "now", "just", "still", "even", "only", "suddenly",
    "finally", "later", "meanwhile", "perhaps", "maybe", "again", "also", "however", "instead", "outside", "inside", "somewhere", "everyone",
    "someone", "nobody", "anyone", "everything", "something", "nothing", "what", "who", "why", "how", "where", "which", "here", "yes", "no",
    "oh", "well", "chapter", "he", "she", "they", "i", "we", "you", "one", "two", "three", "with", "from", "into", "for", "not", "do",
    "did", "does", "was", "were", "is", "are", "had", "have", "has", "be", "been", "very", "all", "some", "any", "each", "both", "such",
}

# --------------------------------------------------------------------------- lexical cues
# word stem -> (emotion, strength 1-3). Matched on word starts, so "furious" and "furiously" both hit.
_CUE_WORDS = {
    "joy": "happy joy delight laugh smil grin cheer glad beam giggl rejoic elat thrill",
    "excitement": "excit eager thrilled rush racing burst bounc cheer wild",
    "tenderness": "gentl soft tender caress cradl kiss embrac fond sweet",
    "sadness": "sad tear cry cried weep sorrow lonely miss mourn sob heartbr melanchol",
    "grief": "grief griev dead death died funeral coffin buried loss lost gone widow bereav",
    "fear": "afraid fear terror terrif scared frighten dread panic horror trembl shak cower",
    "anxiety": "nervous anxious worr uneasy tense fidget apprehens restless doubt",
    "anger": "anger angry furious rage fury snarl glare slam fume hate seeth livid outrag",
    "surprise": "surpris shock stunn gasp astonish startl amaz stared",
    "disgust": "disgust revolt repuls sicken loath vile",
    "determination": "determin resolv vow swore firm steel refus insist",
    "humor": "joke jest wry smirk chuckl teas amus quip",
    "resignation": "sigh weary tired resign shrug given up",
    "calm": "calm quiet still peace serene steady",
}
CUE_STEMS: list[tuple[str, str]] = []
for _emo, _words in _CUE_WORDS.items():
    for _w in _words.split():
        CUE_STEMS.append((_w, _emo))

# Speech-verb colouring: verb -> (emotion or None, intensity delta, pacing or None)
VERB_COLOUR = {
    "whispered": (None, -1, "slow"), "murmured": (None, -1, "slow"), "breathed": ("tenderness", -1, "slow"),
    "shouted": ("excitement", 2, "fast"), "yelled": ("excitement", 2, "fast"), "screamed": ("fear", 3, "fast"), "shrieked": ("fear", 3, "fast"),
    "roared": ("anger", 3, "fast"), "bellowed": ("anger", 3, "fast"), "thundered": ("anger", 3, "brisk"), "barked": ("anger", 2, "brisk"),
    "snapped": ("anger", 1, "brisk"), "growled": ("anger", 1, "measured"), "hissed": ("anger", 1, "brisk"), "snarled": ("anger", 2, "brisk"),
    "sobbed": ("sadness", 2, "slow"), "wailed": ("grief", 3, "slow"), "whimpered": ("fear", 1, "slow"), "pleaded": ("fear", 1, "brisk"),
    "begged": ("fear", 2, "brisk"), "sighed": ("resignation", 0, "slow"), "laughed": ("joy", 1, "brisk"), "chuckled": ("humor", 0, "normal"),
    "gasped": ("surprise", 1, "brisk"), "exclaimed": ("excitement", 1, "brisk"), "stammered": ("anxiety", 1, "measured"),
    "stuttered": ("anxiety", 1, "measured"), "scoffed": ("disgust", 0, "normal"), "sneered": ("disgust", 1, "normal"),
    "groaned": ("sadness", 0, "slow"), "croaked": (None, 0, "slow"), "panted": ("fear", 1, "fast"),
}
ADVERB_COLOUR = {
    "angrily": ("anger", 1), "sadly": ("sadness", 1), "happily": ("joy", 1), "fearfully": ("fear", 1), "nervously": ("anxiety", 1),
    "gently": ("tenderness", 1), "softly": ("tenderness", 0), "coldly": ("disgust", 0), "bitterly": ("anger", 1), "wearily": ("resignation", 1),
    "excitedly": ("excitement", 1), "quietly": ("calm", 0), "firmly": ("determination", 1), "tearfully": ("sadness", 2), "furiously": ("anger", 2),
    "warmly": ("tenderness", 0), "sharply": ("anger", 1), "wryly": ("humor", 0), "desperately": ("fear", 2), "calmly": ("calm", 0),
}
TENSION_WORDS = "danger threat gun knife blood scream run chase trap deadly kill death dark silence footsteps hunted trembl panic fight hurry now quickly".split()

_WORD = re.compile(r"[A-Za-z']+")


def cue_scores(text: str) -> dict[str, float]:
    """Raw per-emotion evidence in a piece of text (word-stem hits, exclamations)."""
    scores: dict[str, float] = {}
    low = text.lower()
    for w in _WORD.findall(low):
        for stem, emo in CUE_STEMS:
            if w.startswith(stem):
                scores[emo] = scores.get(emo, 0.0) + 1.0
                break
    ex = low.count("!")
    if ex:
        scores["excitement"] = scores.get("excitement", 0.0) + 0.4 * min(ex, 3)
    return scores


def tension_score(text: str) -> float:
    """0..5 narrative tension estimate from lexical and rhythmic evidence."""
    words = _WORD.findall(text.lower())
    if not words:
        return 0.0
    hits = sum(1 for w in words if any(w.startswith(t) for t in TENSION_WORDS))
    sentences = max(1, len(re.findall(r"[.!?]+", text)))
    avg_len = len(words) / sentences
    score = hits * 1.2 + text.count("!") * 0.5 + text.count("?") * 0.2 + (1.0 if avg_len < 7 and sentences >= 3 else 0.0)
    return round(min(5.0, score), 2)


def clamp_intensity(v: float | int) -> int:
    return int(max(INTENSITY_MIN, min(INTENSITY_MAX, round(v))))
