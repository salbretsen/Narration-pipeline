"""Text normalisation.

Two different jobs live here and must not be confused:

* ``to_speech`` turns immutable manuscript text into a *pronounceable rendition* for the TTS
  model (numbers, currency, abbreviations...). The original text is never modified; the
  rendition is stored separately and an operator override always wins.
* ``compare_tokens`` canonicalises text for ASR comparison so that harmless differences
  (punctuation, "1,000" vs "one thousand", "don't" vs "do not", British/American spelling)
  are not reported as narration errors.
"""
from __future__ import annotations

import re
import unicodedata

ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand"))
_IRREGULAR_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth", "twelve": "twelfth"}


def int_to_words(n: int) -> str:
    if n < 0:
        return "minus " + int_to_words(-n)
    if n < 20:
        return ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return TENS[t] + ("-" + ONES[o] if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return ONES[h] + " hundred" + (" " + int_to_words(r) if r else "")
    for value, name in SCALES:
        if n >= value:
            q, r = divmod(n, value)
            return int_to_words(q) + " " + name + (" " + int_to_words(r) if r else "")
    return str(n)


def ordinal_words(n: int) -> str:
    words = int_to_words(n)
    head, sep, last = words.rpartition(" ")
    h2, sep2, last2 = last.rpartition("-")
    if last2 in _IRREGULAR_ORD:
        last2 = _IRREGULAR_ORD[last2]
    elif last2.endswith("y"):
        last2 = last2[:-1] + "ieth"
    else:
        last2 += "th"
    last = f"{h2}{sep2}{last2}"
    return f"{head}{sep}{last}"


def year_to_words(n: int) -> str:
    if 2000 <= n <= 2009:
        return "two thousand" + (" " + ONES[n - 2000] if n > 2000 else "")
    hi, lo = divmod(n, 100)
    if lo == 0:
        return int_to_words(hi) + " hundred"
    if lo < 10:
        return int_to_words(hi) + " oh " + ONES[lo]
    return int_to_words(hi) + " " + int_to_words(lo)


def _plural_decade(words: str) -> str:
    if words.endswith("y"):
        return words[:-1] + "ies"
    return words + "s"


ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def roman_to_int(s: str) -> int | None:
    s = s.lower()
    if not s or any(ch not in ROMAN for ch in s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        v = ROMAN[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total if 0 < total < 4000 else None


_SPEECH_ABBREV = [
    (re.compile(r"\bMr\.(?=\s)"), "Mister"),
    (re.compile(r"\bMrs\.(?=\s)"), "Missus"),
    (re.compile(r"\bMs\.(?=\s)"), "Miz"),
    (re.compile(r"\bDr\.(?=\s+[A-Z])"), "Doctor"),
    (re.compile(r"\bProf\.(?=\s+[A-Z])"), "Professor"),
    (re.compile(r"\be\.g\.(?=[\s,])", re.I), "for example"),
    (re.compile(r"\bi\.e\.(?=[\s,])", re.I), "that is"),
    (re.compile(r"\betc\.", re.I), "et cetera."),
    (re.compile(r"\bvs\.?(?=\s)", re.I), "versus"),
    (re.compile(r"\bNo\.\s*(?=\d)"), "number "),
]
_CURRENCY = {"$": ("dollar", "dollars", "cent", "cents"), "£": ("pound", "pounds", "penny", "pence"), "€": ("euro", "euros", "cent", "cents")}


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def to_speech(text: str) -> str:
    """Pronounceable rendition of manuscript text for TTS input."""
    t = text
    t = re.sub(r"\[(?:\d{1,3}|[a-z])\]", "", t)  # footnote markers such as [12]
    t = t.replace("&", " and ")
    for rx, rep in _SPEECH_ABBREV:
        t = rx.sub(rep, t)

    def currency(m):
        sym, whole, cents = m.group(1), _num(m.group(2)), m.group(3)
        one, many, c1, cmany = _CURRENCY[sym]
        out = f"{int_to_words(whole)} {one if whole == 1 else many}"
        if cents and int(cents):
            c = int(cents)
            out += f" and {int_to_words(c)} {c1 if c == 1 else cmany}"
        return out

    t = re.sub(r"([$£€])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?\b", currency, t)
    t = re.sub(r"(\d+(?:\.\d+)?)\s?%", lambda m: f"{m.group(1)} percent", t)
    t = re.sub(r"\b(\d+)(st|nd|rd|th)\b", lambda m: ordinal_words(int(m.group(1))), t)
    t = re.sub(r"\b(1[1-9]\d0|20[0-9]0)s\b", lambda m: _plural_decade(year_to_words(int(m.group(1)))), t)
    t = re.sub(r"(?<![\d,.$])\b(1[1-9]\d\d|20\d\d)\b(?![,.]\d)", lambda m: year_to_words(int(m.group(1))), t)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", _clock, t)
    t = re.sub(r"(\d)\s?[–—]\s?(\d)", r"\1 to \2", t)
    t = re.sub(r"\b(\d+)\.(\d+)\b", lambda m: f"{int_to_words(int(m.group(1)))} point {' '.join(ONES[int(d)] for d in m.group(2))}", t)
    t = re.sub(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{1,15}\b", lambda m: int_to_words(_num(m.group(0))), t)
    t = re.sub(r"\b(Chapter|Part|Book)\s+([IVXLCDM]+)\b", lambda m: f"{m.group(1)} {int_to_words(roman_to_int(m.group(2)) or 0)}" if roman_to_int(m.group(2)) else m.group(0), t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def _clock(m) -> str:
    h, mm = int(m.group(1)), int(m.group(2))
    if h > 24 or mm > 59:
        return m.group(0)
    if mm == 0:
        return f"{int_to_words(h)} o'clock"
    if mm < 10:
        return f"{int_to_words(h)} oh {ONES[mm]}"
    return f"{int_to_words(h)} {int_to_words(mm)}"


# ------------------------------------------------------------------ comparison

CONTRACTIONS = {
    "won't": "will not", "can't": "can not", "cannot": "can not", "shan't": "shall not", "ain't": "is not",
    "let's": "let us", "it's": "it is", "that's": "that is", "what's": "what is", "there's": "there is",
    "here's": "here is", "who's": "who is", "he's": "he is", "she's": "she is", "where's": "where is",
    "how's": "how is", "i'm": "i am", "y'all": "you all", "o'clock": "oclock", "ma'am": "madam",
}
_SUFFIXES = (("n't", " not"), ("'re", " are"), ("'ve", " have"), ("'ll", " will"), ("'d", " would"))
TOKEN_MAP = {
    "mr": "mister", "mrs": "missus", "ms": "miz", "dr": "doctor", "prof": "professor", "ok": "okay",
    "st": "saint", "vs": "versus", "etc": "et cetera", "colour": "color", "favour": "favor",
    "honour": "honor", "labour": "labor", "neighbour": "neighbor", "grey": "gray", "theatre": "theater",
    "centre": "center", "metre": "meter", "travelled": "traveled", "travelling": "traveling",
    "towards": "toward", "afterwards": "afterward", "learnt": "learned", "spelt": "spelled",
    "dreamt": "dreamed", "burnt": "burned", "whilst": "while", "amongst": "among", "alright": "all right",
    "mum": "mom", "realise": "realize", "realised": "realized", "organise": "organize", "jewellery": "jewelry",
    "percent": "percent", "per": "per", "cent": "cent",
}
_SMALL = {w: i for i, w in enumerate(ONES)}
_TENS_PLURAL = {w[:-1] + "ies": w for w in TENS if w}  # "nineties" -> "ninety" (decades)
_TENS_W = {w: i * 10 for i, w in enumerate(TENS) if w}
_MULT = {"hundred": 100, "thousand": 1000, "million": 10**6, "billion": 10**9, "trillion": 10**12}
_ORD_WORDS = {ordinal_words(i): i for i in range(1, 100)}
_ORD_WORDS.update({"hundredth": 100, "thousandth": 1000})


def _ord_suffix(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}" + {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _words_to_numbers(tokens: list[str]) -> list[str]:
    """Collapse runs of spelled-out numbers into digits ("twenty one" -> "21",
    "nineteen ninety nine" -> "1999", "two thousand and five" -> "2005", "third" -> "3rd")."""
    out: list[str] = []
    i = 0
    n = len(tokens)

    def is_num(w: str) -> bool:
        return w in _SMALL or w in _TENS_W or w in _MULT

    while i < n:
        w = tokens[i]
        if w in _ORD_WORDS and w not in ("second",):
            out.append(_ord_suffix(_ORD_WORDS[w]))
            i += 1
            continue
        if not is_num(w) or (w in _MULT and not out and False):
            out.append(w)
            i += 1
            continue
        # collect run
        run: list[str] = []
        j = i
        while j < n:
            t = tokens[j]
            if is_num(t):
                run.append(t)
                j += 1
            elif t == "and" and run and j + 1 < n and is_num(tokens[j + 1]) and any(r in _MULT for r in run):
                j += 1
            elif t == "oh" and run and j + 1 < n and tokens[j + 1] in _SMALL and _SMALL[tokens[j + 1]] < 10:
                run.append("oh")
                j += 1
            else:
                break
        # ordinal tail ("twenty first")
        ord_tail = None
        if j < n and tokens[j] in _ORD_WORDS and _ORD_WORDS[tokens[j]] < 10 and run and run[-1] in _TENS_W:
            ord_tail = _ORD_WORDS[tokens[j]]
            j += 1
        numbers = _parse_number_run(run)
        if ord_tail is not None and numbers:
            numbers[-1] = _ord_suffix(int(numbers[-1]) + ord_tail)
        out.extend(numbers)
        i = j
    return out


def _parse_number_run(run: list[str]) -> list[str]:
    groups: list[int | str] = []
    total = 0
    part = 0
    started = False
    has_mult = False

    def emit():
        nonlocal total, part, started, has_mult
        if started:
            groups.append(total + part)
        total, part, started, has_mult = 0, 0, False, False

    k = 0
    while k < len(run):
        w = run[k]
        if w == "oh":
            # "eighteen oh five" -> 1805
            if started and k + 1 < len(run):
                prefix = total + part
                nxt = _SMALL.get(run[k + 1], 0)
                total, part = 0, prefix * 100 + nxt
                k += 2
                continue
            k += 1
            continue
        if w in _MULT:
            m = _MULT[w]
            if m == 100:
                part = (part or 1) * 100
            else:
                total += (part or 1) * m
                part = 0
            started = True
            has_mult = True
            k += 1
            continue
        v = _SMALL.get(w, _TENS_W.get(w))
        if not started:
            part, started = v, True
        elif part % 100 == 0 and part and v < 100 and part >= 100:
            part += v  # "one hundred five"
        elif 20 <= part % 100 and part % 10 == 0 and v < 10 and (part % 100) != 0:
            part += v  # "twenty one"
        elif total and part == 0:
            part = v  # "two thousand five"
        elif not has_mult and 10 <= part < 100 and v < 100:
            # year-like pair "nineteen ninety" / "twenty twenty"
            part = part * 100 + v
            has_mult = True
        else:
            emit()
            part, started = v, True
        k += 1
    emit()
    return [str(g) for g in groups]


def compare_tokens(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("`", "'")
    t = re.sub(r"\[(?:\d{1,3}|[a-z])\]", " ", t)
    t = t.replace("&", " and ")
    t = re.sub(r"\bno\.\s*(?=\d)", "number ", t)
    t = re.sub(r"\b(chapter|part|book)\s+([ivxlcdm]+)\b", lambda m: f"{m.group(1)} {roman_to_int(m.group(2)) or m.group(2)}", t)
    t = re.sub(r"\$\s?(\d[\d,]*)\.(\d\d)\b", r"\1 dollars and \2 cents", t)
    t = re.sub("£\\s?(\\d[\\d,]*)\\.(\\d\\d)\\b", r"\1 pounds and \2 pence", t)
    t = re.sub("€\\s?(\\d[\\d,]*)\\.(\\d\\d)\\b", r"\1 euros and \2 cents", t)
    t = re.sub(r"\$\s?(\d[\d,]*(?:\.\d+)?)", r"\1 dollars", t)
    t = re.sub("£\\s?(\\d[\\d,]*(?:\\.\\d+)?)", r"\1 pounds", t)
    t = re.sub("€\\s?(\\d[\\d,]*(?:\\.\\d+)?)", r"\1 euros", t)
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)
    t = re.sub(r"(\d)\s*%", r"\1 percent", t)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", lambda m: m.group(1) + (" oclock" if m.group(2) == "00" else " " + str(int(m.group(2)))), t)
    t = re.sub(r"(\d+)\.(\d+)", r"\1 point \2", t)
    t = re.sub(r"[\-‐-―/]", " ", t)
    words = []
    for w in t.split():
        w = w.strip(".,;:!?\"()[]{}<>*_~…")
        w = w.strip("'")
        if not w:
            continue
        if w in CONTRACTIONS:
            words.extend(CONTRACTIONS[w].split())
            continue
        for suf, rep in _SUFFIXES:
            if w.endswith(suf) and len(w) > len(suf):
                words.extend((w[: -len(suf)] + rep).split())
                break
        else:
            words.append(w)
    cleaned: list[str] = []
    for w in words:
        w = re.sub(r"[^\w']", "", w).replace("'", "")
        if not w:
            continue
        w = TOKEN_MAP.get(w, w)
        w = _TENS_PLURAL.get(w, w)
        cleaned.extend(w.split())
    cleaned = _words_to_numbers(cleaned)
    # "1990s" == "1990 s"
    final: list[str] = []
    for w in cleaned:
        m = re.fullmatch(r"(\d+)(st|nd|rd|th|s)", w)
        if m and m.group(2) == "s":
            final.append(m.group(1))
            continue
        final.append(w)
    return final
