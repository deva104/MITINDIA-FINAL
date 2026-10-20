"""One normaliser for vehicle make, model, colour and body type.

RCs print raw text ("TOYOTA KIRLOSKAR MOTOR PVT LTD", "GRANDI10NIOS1.2MTKAPPASP",
"PEARL ARCTIC WHITE", "SALOON"), photos are described in words ("teal-blue
hatchback") and the registry has its own spelling. Every comparison of these
goes through here. A value this module does not recognise is None, and a
comparison with None on either side is skipped: no flag, no "could not
determine" entry.
"""
import re

# --- make ---------------------------------------------------------------------

CORPORATE_WORDS = {"ltd", "limited", "pvt", "private", "india", "motor", "motors", "co",
                   "company", "corporation", "corp", "inc", "the", "and"}
MAKE_ALIASES = {
    "maruti": "maruti suzuki", "suzuki": "maruti suzuki", "msil": "maruti suzuki",
    "hyundai": "hyundai", "tata": "tata", "mahindra": "mahindra", "toyota": "toyota",
    "honda": "honda", "kia": "kia", "mg": "mg", "renault": "renault", "nissan": "nissan",
    "datsun": "datsun", "skoda": "skoda", "volkswagen": "volkswagen", "vw": "volkswagen",
    "ford": "ford", "jeep": "jeep", "bmw": "bmw", "mercedes": "mercedes-benz",
    "benz": "mercedes-benz", "audi": "audi", "citroen": "citroen", "byd": "byd",
    "fiat": "fiat", "chevrolet": "chevrolet", "mitsubishi": "mitsubishi", "isuzu": "isuzu",
    "force": "force", "volvo": "volvo", "lexus": "lexus", "jaguar": "jaguar",
    "rover": "land rover", "porsche": "porsche", "subaru": "subaru", "peugeot": "peugeot",
    "mazda": "mazda", "tesla": "tesla", "opel": "opel", "dodge": "dodge",
}
# Model names (spacing and case removed) and their make, so "Brezza" alone
# names Maruti Suzuki. Longest names first when one contains another.
MODELS = {
    "maruti suzuki": ("vitarabrezza", "brezza", "grandvitara", "swift", "dzire", "baleno",
                      "ertiga", "alto", "wagonr", "celerio", "ignis", "ciaz", "xl6", "fronx",
                      "jimny", "eeco", "spresso", "invicto", "sx4", "ritz", "zen", "esteem",
                      "gypsy", "omni"),
    "hyundai": ("grandi10nios", "grandi10", "i10", "i20", "aura", "venue", "creta", "verna",
                "alcazar", "tucson", "exter", "santro", "xcent", "eon", "elantra", "kona"),
    "tata": ("indicavista", "indica", "indigo", "nexon", "tiago", "tigor", "altroz", "punch",
             "harrier", "safari", "nano", "zest", "bolt", "hexa", "sumo", "aria", "curvv"),
    "mahindra": ("bolero", "scorpio", "xuv300", "xuv3xo", "xuv400", "xuv500", "xuv700", "thar",
                 "kuv100", "marazzo", "tuv300", "xylo", "quanto", "verito"),
    "toyota": ("corollaaltis", "corolla", "innovacrysta", "innova", "fortuner", "etiosliva",
               "etios", "glanza", "urbancruiser", "hyryder", "camry", "qualis", "rumion"),
    "honda": ("city", "amaze", "jazz", "wrv", "brv", "civic", "elevate", "brio", "mobilio"),
    "kia": ("seltos", "sonet", "carens", "carnival", "syros"),
    "renault": ("kwid", "triber", "kiger", "duster", "lodgy"),
    "nissan": ("magnite", "kicks", "micra", "sunny", "terrano"),
    "ford": ("ecosport", "figo", "aspire", "freestyle", "endeavour", "fiesta", "ikon"),
    "volkswagen": ("polo", "vento", "virtus", "taigun", "ameo", "tiguan"),
    "skoda": ("rapid", "slavia", "kushaq", "octavia", "superb", "kodiaq", "laura", "fabia", "yeti"),
    "mg": ("hector", "astor", "zsev", "comet", "gloster", "windsor"),
    "jeep": ("compass", "meridian", "wrangler"),
}
MODEL_MAKE = {name: make for make, names in MODELS.items() for name in names}
MODEL_NAMES = sorted(MODEL_MAKE, key=len, reverse=True)


def _words(text):
    return re.findall(r"[a-z0-9]+", str(text or "").lower().replace("&", " and "))


def _compact(text):
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def make(text):
    """The make a text names ("TOYOTA KIRLOSKAR MOTOR PVT LTD" -> "toyota"), or
    the make of a model it names ("Brezza" -> "maruti suzuki"); else None."""
    raw = str(text or "").lower()
    if re.search(r"\bm\s*&\s*m\b", raw) or re.search(r"\bmahindra\s*(&|and)\s*mahindra\b", raw):
        return "mahindra"
    for word in _words(raw):
        if word in CORPORATE_WORDS:
            continue
        if word in MAKE_ALIASES:
            return MAKE_ALIASES[word]
    names = models(text)
    return MODEL_MAKE[names[0]] if names else None


def models(text):
    """Known model names in a text, longest first, none inside another found
    one: "GRANDI10NIOS1.2MTKAPPASP" -> ["grandi10nios"]."""
    compact = _compact(text)
    found = []
    for name in MODEL_NAMES:
        if name in compact and not any(name in longer for longer in found):
            found.append(name)
    return found


def model_relation(a, b):
    """"same", "different" or None (not comparable). Spacing and case are
    ignored: "V BREZZA ZXI" = "Vitara Brezza" = "Brezza", "COROLLA ALTIS D4D G"
    = "Corolla Altis"."""
    left, right = models(a), models(b)
    if not left or not right:
        return None
    if set(left) & set(right) or any(x in y or y in x for x in left for y in right):
        return "same"
    return "different"


# --- colour -------------------------------------------------------------------

# Base colour words decide the family; modifiers ("pearl", "metallic", "arctic")
# only when no base word is present, so "PEARL MIDNIGHT BLACK" is black.
COLOUR_FAMILIES = {
    "white": "white", "ivory": "white", "cream": "white",
    "silver": "silver", "grey": "grey", "gray": "grey", "graphite": "grey", "gunmetal": "grey",
    "black": "black",
    "red": "red", "maroon": "red", "wine": "red", "burgundy": "red", "cherry": "red",
    "blue": "blue", "navy": "blue", "teal": "blue", "turquoise": "blue", "aqua": "blue",
    "cyan": "blue", "azure": "blue",
    "green": "green", "olive": "green",
    "brown": "brown", "beige": "brown", "gold": "brown", "golden": "brown", "bronze": "brown",
    "copper": "brown", "champagne": "brown",
    "orange": "orange", "yellow": "yellow", "purple": "purple", "violet": "purple",
}
COLOUR_MODIFIERS = {"pearl": "white", "arctic": "white", "diamond": "white", "snow": "white"}
# Families a camera or a printed name can confuse: a near match, never a flag.
NEAR_COLOURS = {frozenset(("silver", "grey")), frozenset(("blue", "green"))}


def colour(text):
    """The colour family ("TEAL BLUE" -> blue, "M-SILVER" -> silver,
    "PEARL ARCTIC WHITE" -> white), or None."""
    words = _words(text)
    for word in words:
        if word in COLOUR_FAMILIES:
            return COLOUR_FAMILIES[word]
    for word in words:
        if word in COLOUR_MODIFIERS:
            return COLOUR_MODIFIERS[word]
    return None


def colour_relation(a, b):
    """"same", "near", "different" or None. Only "different" may raise a flag."""
    left, right = colour(a), colour(b)
    if not left or not right:
        return None
    if left == right:
        return "same"
    return "near" if frozenset((left, right)) in NEAR_COLOURS else "different"


# --- body type ----------------------------------------------------------------

# Checked in order on the text with spaces and punctuation removed. SUV,
# crossover, MUV, MPV and "hard top" are one class. "RIGID (PASSENGER CAR)"
# is the RC's vehicle category, not a body style: unknown.
BODY_PATTERNS = (
    ("hatchback", ("hatchback", "hatch")),
    ("sedan", ("sedan", "saloon", "notchback")),
    ("suv", ("compactsuv", "suv", "crossover", "muv", "mpv", "hardtop", "softtop")),
    ("pickup", ("pickup",)),
    ("van", ("van",)),
    ("truck", ("truck", "lorry")),
)


def body(text):
    compact = _compact(text)
    if not compact:
        return None
    for name, patterns in BODY_PATTERNS:
        if any(pattern in compact for pattern in patterns):
            return name
    return None


def body_relation(a, b):
    left, right = body(a), body(b)
    if not left or not right:
        return None
    return "same" if left == right else "different"


def make_relation(a, b):
    left, right = make(a), make(b)
    if not left or not right:
        return None
    return "same" if left == right else "different"
