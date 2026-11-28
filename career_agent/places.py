"""Where a job is and how far that is from home: measured here, never guessed by a model.

Models guess distances badly (the same town came out as 50, 60 and 80 km from home in one run), and a guess that
breaks your distance limit sinks an otherwise good job. So the app works the distance out itself, from a list of
Danish towns, and tells the model the number.

The distance is the straight line from your town to the job's town. Where there is water between (Jylland to Fyn,
Fyn to Sjælland) it is measured over the bridge. That is never more than the drive, so "outside your limit" is
always true by road as well; the drive itself is roughly a quarter longer.
"""
from __future__ import annotations

import math
import re
import unicodedata

# (name, region, latitude, longitude, postal codes, other spellings). Regions: J = Jylland, F = Fyn, Z = Sjælland.
_TOWNS: list[tuple[str, str, float, float, tuple[int, ...], tuple[str, ...]]] = [
    # East Jylland
    ("Horsens", "J", 55.861, 9.850, (8700,), ()),
    ("Aarhus", "J", 56.157, 10.211, (8000,), ("århus", "aarhus c", "århus c")),
    ("Aarhus N", "J", 56.190, 10.190, (8200,), ("århus n", "skejby")),
    ("Aarhus V", "J", 56.170, 10.160, (8210,), ("århus v",)),
    ("Brabrand", "J", 56.155, 10.107, (8220,), ()),
    ("Åbyhøj", "J", 56.153, 10.165, (8230,), ("aabyhøj",)),
    ("Risskov", "J", 56.190, 10.230, (8240,), ()),
    ("Egå", "J", 56.213, 10.270, (8250,), ("egaa",)),
    ("Viby J", "J", 56.126, 10.160, (8260,), ("viby j.", "viby ved aarhus", "viby")),
    ("Højbjerg", "J", 56.110, 10.210, (8270,), ()),
    ("Odder", "J", 55.975, 10.150, (8300,), ()),
    ("Tranbjerg", "J", 56.090, 10.130, (8310,), ()),
    ("Mårslet", "J", 56.070, 10.160, (8320,), ()),
    ("Beder", "J", 56.060, 10.210, (8330,), ()),
    ("Malling", "J", 56.040, 10.200, (8340,), ()),
    ("Hasselager", "J", 56.106, 10.106, (8361,), ()),
    ("Hørning", "J", 56.085, 10.037, (8362,), ()),
    ("Hadsten", "J", 56.328, 10.050, (8370,), ()),
    ("Tilst", "J", 56.190, 10.110, (8381,), ()),
    ("Hinnerup", "J", 56.266, 10.063, (8382,), ()),
    ("Ebeltoft", "J", 56.194, 10.680, (8400,), ()),
    ("Rønde", "J", 56.301, 10.475, (8410,), ()),
    ("Hammel", "J", 56.257, 9.863, (8450,), ()),
    ("Galten", "J", 56.153, 9.907, (8464,), ()),
    ("Sabro", "J", 56.213, 10.035, (8471,), ()),
    ("Grenaa", "J", 56.416, 10.878, (8500,), ("grenå",)),
    ("Lystrup", "J", 56.236, 10.238, (8520,), ()),
    ("Hjortshøj", "J", 56.250, 10.265, (8530,), ()),
    ("Skødstrup", "J", 56.265, 10.310, (8541,), ()),
    ("Hornslet", "J", 56.316, 10.320, (8543,), ()),
    ("Silkeborg", "J", 56.170, 9.545, (8600,), ()),
    ("Kjellerup", "J", 56.285, 9.430, (8620,), ()),
    ("Them", "J", 56.095, 9.545, (8653,), ()),
    ("Skanderborg", "J", 56.040, 9.930, (8660,), ()),
    ("Stilling", "J", 56.062, 10.000, (), ()),
    ("Ry", "J", 56.090, 9.765, (8680,), ()),
    ("Daugård", "J", 55.730, 9.720, (8721,), ()),
    ("Hedensted", "J", 55.772, 9.701, (8722,), ()),
    ("Løsning", "J", 55.802, 9.702, (8723,), ()),
    ("Hovedgård", "J", 55.946, 9.960, (8732,), ()),
    ("Brædstrup", "J", 55.970, 9.610, (8740,), ()),
    ("Gedved", "J", 55.930, 9.850, (8751,), ()),
    ("Østbirk", "J", 55.965, 9.760, (8752,), ()),
    ("Nørre Snede", "J", 55.960, 9.400, (8766,), ("nr. snede",)),
    ("Viborg", "J", 56.453, 9.402, (8800,), ()),
    ("Bjerringbro", "J", 56.377, 9.660, (8850,), ()),
    ("Fårvang", "J", 56.267, 9.720, (8882,), ("faarvang",)),
    ("Randers", "J", 56.460, 10.036, (8900, 8920, 8930, 8940, 8960), ()),
    # South-east and south Jylland
    ("Fredericia", "J", 55.566, 9.752, (7000,), ()),
    ("Taulov", "J", 55.546, 9.615, (), ()),
    ("Børkop", "J", 55.640, 9.650, (7080,), ()),
    ("Vejle", "J", 55.709, 9.536, (7100, 7120), ()),
    ("Juelsminde", "J", 55.710, 10.010, (7130,), ()),
    ("Stouby", "J", 55.710, 9.800, (7140,), ()),
    ("Tørring", "J", 55.850, 9.480, (7160,), ()),
    ("Uldum", "J", 55.850, 9.600, (7171,), ()),
    ("Billund", "J", 55.731, 9.115, (7190,), ()),
    ("Grindsted", "J", 55.757, 8.930, (7200,), ()),
    ("Jelling", "J", 55.756, 9.420, (7300,), ()),
    ("Give", "J", 55.845, 9.238, (7323,), ()),
    ("Thyregod", "J", 55.905, 9.250, (), ()),
    ("Brande", "J", 55.943, 9.127, (7330,), ()),
    ("Kolding", "J", 55.490, 9.472, (6000,), ()),
    ("Egtved", "J", 55.616, 9.310, (6040,), ()),
    ("Christiansfeld", "J", 55.357, 9.487, (6070,), ()),
    ("Haderslev", "J", 55.250, 9.490, (6100,), ()),
    ("Aabenraa", "J", 55.044, 9.418, (6200,), ("åbenrå",)),
    ("Tønder", "J", 54.933, 8.867, (6270,), ()),
    ("Sønderborg", "J", 54.913, 9.792, (6400,), ()),
    ("Nordborg", "J", 55.057, 9.741, (6430,), ()),
    ("Gram", "J", 55.290, 9.050, (6510,), ()),
    ("Vamdrup", "J", 55.428, 9.283, (6580,), ()),
    ("Vejen", "J", 55.480, 9.140, (6600,), ()),
    ("Esbjerg", "J", 55.476, 8.460, (6700, 6705, 6710, 6715), ()),
    ("Ribe", "J", 55.328, 8.762, (6760,), ()),
    ("Varde", "J", 55.621, 8.481, (6800,), ()),
    ("Skjern", "J", 55.950, 8.495, (6900,), ()),
    ("Ringkøbing", "J", 56.090, 8.244, (6950,), ()),
    # Mid, west and north Jylland
    ("Herning", "J", 56.139, 8.974, (7400,), ()),
    ("Ikast", "J", 56.139, 9.158, (7430,), ()),
    ("Holstebro", "J", 56.360, 8.616, (7500,), ()),
    ("Struer", "J", 56.491, 8.584, (7600,), ()),
    ("Lemvig", "J", 56.549, 8.310, (7620,), ()),
    ("Thisted", "J", 56.955, 8.694, (7700,), ()),
    ("Skive", "J", 56.566, 9.027, (7800,), ()),
    ("Nykøbing Mors", "J", 56.795, 8.860, (7900,), ()),
    ("Aalborg", "J", 57.048, 9.919, (9000, 9200, 9210, 9220), ("ålborg",)),
    ("Nørresundby", "J", 57.060, 9.920, (9400,), ()),
    ("Hobro", "J", 56.643, 9.790, (9500,), ()),
    ("Støvring", "J", 56.887, 9.829, (9530,), ()),
    ("Aars", "J", 56.803, 9.518, (9600,), ("års",)),
    ("Brønderslev", "J", 57.270, 9.940, (9700,), ()),
    ("Hjørring", "J", 57.464, 9.982, (9800,), ()),
    ("Frederikshavn", "J", 57.441, 10.537, (9900,), ()),
    ("Skagen", "J", 57.720, 10.580, (9990,), ()),
    # Fyn
    ("Middelfart", "F", 55.506, 9.731, (5500,), ()),
    ("Odense", "F", 55.396, 10.388, (5000, 5200, 5210, 5220, 5230, 5240, 5250, 5260, 5270), ()),
    ("Bogense", "F", 55.567, 10.089, (5400,), ()),
    ("Assens", "F", 55.270, 9.900, (5610,), ()),
    ("Faaborg", "F", 55.095, 10.242, (5600,), ("fåborg",)),
    ("Svendborg", "F", 55.060, 10.607, (5700,), ()),
    ("Ringe", "F", 55.238, 10.478, (5750,), ()),
    ("Nyborg", "F", 55.312, 10.790, (5800,), ()),
    ("Kerteminde", "F", 55.449, 10.657, (5300,), ()),
    # Sjælland
    ("København", "Z", 55.676, 12.568, (), ("copenhagen", "kbh", "københavn k", "københavn v", "københavn ø",
                                            "københavn n", "københavn s", "københavn nv", "københavn sv",
                                            "nordhavn", "ørestad", "valby", "storkøbenhavn", "greater copenhagen")),
    ("Frederiksberg", "Z", 55.678, 12.533, (), ()),
    ("Kastrup", "Z", 55.635, 12.650, (), ()),
    ("Hvidovre", "Z", 55.643, 12.474, (), ()),
    ("Brøndby", "Z", 55.650, 12.420, (), ()),
    ("Glostrup", "Z", 55.666, 12.400, (), ()),
    ("Albertslund", "Z", 55.657, 12.354, (), ()),
    ("Taastrup", "Z", 55.652, 12.292, (), ("høje taastrup", "tåstrup")),
    ("Ishøj", "Z", 55.615, 12.350, (), ()),
    ("Greve", "Z", 55.583, 12.300, (), ()),
    ("Herlev", "Z", 55.724, 12.440, (), ()),
    ("Skovlunde", "Z", 55.720, 12.400, (), ()),
    ("Ballerup", "Z", 55.731, 12.363, (), ()),
    ("Måløv", "Z", 55.750, 12.320, (), ()),
    ("Bagsværd", "Z", 55.762, 12.455, (), ()),
    ("Søborg", "Z", 55.737, 12.512, (), ()),
    ("Gentofte", "Z", 55.750, 12.550, (), ()),
    ("Hellerup", "Z", 55.732, 12.570, (), ()),
    ("Lyngby", "Z", 55.770, 12.503, (), ("kongens lyngby", "kgs. lyngby", "kgs lyngby")),
    ("Farum", "Z", 55.810, 12.360, (), ()),
    ("Birkerød", "Z", 55.847, 12.428, (), ()),
    ("Allerød", "Z", 55.870, 12.358, (), ()),
    ("Hørsholm", "Z", 55.881, 12.501, (), ()),
    ("Hillerød", "Z", 55.927, 12.300, (), ()),
    ("Helsingør", "Z", 56.036, 12.613, (), ()),
    ("Roskilde", "Z", 55.642, 12.080, (), ()),
    ("Køge", "Z", 55.458, 12.182, (), ()),
    ("Ringsted", "Z", 55.443, 11.790, (), ()),
    ("Slagelse", "Z", 55.403, 11.354, (), ()),
    ("Korsør", "Z", 55.330, 11.139, (), ()),
    ("Næstved", "Z", 55.230, 11.760, (), ()),
    ("Holbæk", "Z", 55.717, 11.713, (), ()),
    ("Kalundborg", "Z", 55.680, 11.089, (), ()),
    ("Vordingborg", "Z", 55.008, 11.911, (), ()),
    ("Nykøbing Falster", "Z", 54.769, 11.874, (), ()),
    # Found in ads on 2026-11-28 that the list didn't know (jobs without a distance rank as if in Aarhus)
    ("Hatting", "J", 55.833, 9.767, (), ()),
    ("Holme", "J", 56.113, 10.175, (), ()),  # in Aarhus, next to Højbjerg
    ("Låsby", "J", 56.153, 9.815, (), ()),
    ("Bording", "J", 56.158, 9.243, (), ()),
    ("Karup", "J", 56.307, 9.167, (7470,), ("karup j",)),
    ("Aulum", "J", 56.265, 8.787, (), ()),
    ("Videbæk", "J", 56.088, 8.630, (), ()),
    ("Spjald", "J", 56.120, 8.500, (), ()),
    ("Ølgod", "J", 55.807, 8.623, (), ()),
    ("Tjæreborg", "J", 55.465, 8.578, (), ()),
    ("Tinglev", "J", 54.937, 9.253, (), ()),
    ("Gråsten", "J", 54.919, 9.595, (6300,), ()),
    ("Glyngøre", "J", 56.762, 8.866, (), ()),
    ("Svenstrup J", "J", 56.977, 9.857, (), ("svenstrup",)),
    ("Vrå", "J", 57.354, 9.941, (), ()),
    ("Værløse", "Z", 55.782, 12.370, (3500,), ()),
    ("Frederikssund", "Z", 55.839, 12.069, (3600,), ()),
    ("Rødovre", "Z", 55.681, 12.453, (2610,), ()),
    ("Vallensbæk Strand", "Z", 55.618, 12.383, (2665,), ("vallensbæk",)),
    ("Dragør", "Z", 55.593, 12.671, (2791,), ()),
    ("Hedehusene", "Z", 55.650, 12.197, (2640,), ()),
    ("Nørre Alslev", "Z", 54.893, 11.886, (), ("nr. alslev", "nr alslev")),
    ("Rønne", "Z", 55.100, 14.706, (3700,), ()),
]

# Areas an ad names instead of a town, measured to a town in the middle of them. Wider ones (Midtjylland,
# Sønderjylland) are left unknown: the job could be anywhere in them.
_AREAS = {"københavns omegn": "København", "storkøbenhavn": "København", "nordsjælland": "Hillerød",
          "trekantsområdet": "Fredericia", "trekantområdet": "Fredericia", "bornholm": "Rønne",
          "falster": "Nykøbing Falster"}

# Postal codes not listed above: whole ranges that are far from each other but one area, good enough for "how far".
_RANGES = [(1000, 2999, "København"), (3000, 3699, "Hillerød"), (4000, 4999, "Ringsted"), (5000, 5399, "Odense"),
           (5600, 5999, "Svendborg"), (9000, 9999, "Aalborg")]
_ORDER = "JFZ"  # west to east; one bridge between each pair of neighbours
_BRIDGES = [(55.520, 9.710), (55.340, 11.030)]  # Lillebælt (Jylland–Fyn), Storebælt (Fyn–Sjælland)

_ABROAD = (
    "sweden sverige norway norge germany tyskland deutschland poland polen united kingdom uk england scotland "
    "storbritannien belgium belgien greece grækenland lithuania litauen latvia letland estonia estland netherlands "
    "holland nederlandene france frankrig spain spanien portugal italy italien romania rumænien bulgaria bulgarien "
    "czech tjekkiet czechia vietnam india indien usa finland ireland irland switzerland schweiz austria østrig "
    "hungary ungarn ukraine philippines malaysia singapore china kina japan canada australia turkey tyrkiet serbia "
    "croatia slovakia luxembourg iceland greenland grønland "
    "stockholm gothenburg göteborg malmö malmø lund karlshamn oslo bergen helsinki berlin hamburg munich münchen "
    "flensburg flensborg warsaw warszawa krakow kraków gdansk gdańsk wroclaw vilnius kaunas riga tallinn london "
    "leeds manchester edinburgh brussels bruxelles amsterdam rotterdam paris madrid barcelona lisbon lissabon "
    "athens patras bucharest sofia prague prag budapest vienna wien zurich zürich dublin hanoi bangalore "
    "kuala manila porto lissabon eindhoven wroclaw wrocław pecs pécs birmingham warrington cheshire derby southampton "
    "meyrin nuuk thessaloniki belgrade beograd trondheim strasbourg szczecin isernhagen bern thun köln koeln cologne "
    "malaga málaga udlandet").split() + ["united states", "ho chi minh", "new york", "the hague"]
_NOT_A_PLACE = {"", "danmark", "denmark", "dk", "hybrid", "remote", "onsite", "on-site", "on site", "hovedkontor",
                "hq", "headquarters", "kontor", "office", "mulighed for hjemmearbejde", "hybrid position", "øvrige"}
_STREET = re.compile(r"\b\d{1,3}\s?[a-z]?\b")


def _norm(text: str | None) -> str:
    t = (text or "").lower().replace("å", "aa")
    return re.sub(r"\s+", " ", re.sub(r"[^\w.\- ]", " ", t)).strip()


_BY_NAME: dict[str, int] = {}
_BY_CODE: dict[int, int] = {}
for _i, (_name, _region, _lat, _lon, _codes, _other) in enumerate(_TOWNS):
    for _n in (_name, *_other):
        _BY_NAME.setdefault(_norm(_n), _i)
        # written out the way some ads do: Soeborg, Glyngoere, Koebenhavn
        _BY_NAME.setdefault(_norm(_n.lower().replace("æ", "ae").replace("ø", "oe")), _i)
    for _c in _codes:
        _BY_CODE[_c] = _i
for _area, _town_name in _AREAS.items():
    _BY_NAME.setdefault(_norm(_area), _BY_NAME[_norm(_town_name)])
_NAME_RX = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in sorted(_BY_NAME, key=len, reverse=True))
                      + r")(?![\w])")
_ABROAD_RX = re.compile(r"(?<![\w])(" + "|".join(re.escape(_norm(n)) for n in sorted(_ABROAD, key=len, reverse=True))
                        + r")(?![\w])")
_SPLIT = re.compile(r"[,/;()|]| eller | or | og | and | & | – | - | også | also |\+", re.I)


def _fold(text: str | None) -> str:
    """Text the way job titles are compared (companies.norm_name): lower case, accents gone, å as a."""
    s = unicodedata.normalize("NFKD", (text or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^\w]+", " ", s).strip()


def _written_out(name: str) -> str:
    """æ, ø and å as ae, oe and aa, the way repeats are compared (jobs._folded): København as Koebenhavn."""
    return name.lower().replace("æ", "ae").replace("ø", "oe").replace("å", "aa")


_FOLDED_RX = re.compile(r"(?<![\w])(" + "|".join(re.escape(n) for n in sorted(
    {_fold(v) for t in _TOWNS for n in (t[0], *t[5]) for v in (n, _written_out(n))}
    | {_fold(v) for n in _AREAS for v in (n, _written_out(n))}
    | {_fold(v) for n in _ABROAD for v in (n, _written_out(n))}, key=len, reverse=True) if n)
    + r")(?![\w])")
_PLACE_JOINERS = {"or", "eller", "and", "og", "also", "ogsa", "ogsaa", "near", "naer", "nær", "i", "in", "ved"}


def only_places(text: str | None) -> bool:
    """True when the text names places and nothing else: 'Copenhagen, Aarhus or Aalborg', 'Lystrup'."""
    folded = _fold(text)
    rest = _FOLDED_RX.sub(" ", folded)
    return rest != folded and all(w in _PLACE_JOINERS or w in _NOT_A_PLACE for w in rest.split())


def _line(a: tuple[float, float], b: tuple[float, float]) -> float:
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def km_between(a: int, b: int) -> float:
    """From town a to town b in a straight line, over the bridges where there is water between."""
    ta, tb = _TOWNS[a], _TOWNS[b]
    ra, rb = _ORDER.index(ta[1]), _ORDER.index(tb[1])
    via = _BRIDGES[ra:rb] if ra <= rb else list(reversed(_BRIDGES[rb:ra]))
    stops = [(ta[2], ta[3]), *via, (tb[2], tb[3])]
    return sum(_line(stops[i], stops[i + 1]) for i in range(len(stops) - 1))


def _town(part: str) -> int | None:
    m = _NAME_RX.search(part)
    if m:
        return _BY_NAME[m.group(1)]
    code = re.search(r"\b([1-9]\d{3})\b", part)
    if code:
        n = int(code.group(1))
        if n in _BY_CODE:
            return _BY_CODE[n]
        for lo, hi, name in _RANGES:
            if lo <= n <= hi:
                return _BY_NAME[_norm(name)]
    return None


def read(location: str | None) -> dict:
    """What a location text names: {"towns": [index…], "abroad": [name…], "unclear": [part…]}. A street address
    next to a town is not a place of its own; a part that names nothing known is unclear."""
    towns: list[int] = []
    abroad: list[str] = []
    unclear: list[str] = []
    for raw in _SPLIT.split(location or ""):
        part = _norm(raw)
        if part in _NOT_A_PLACE:
            continue
        t = _town(part)
        if t is not None:
            if t not in towns:
                towns.append(t)
            continue
        m = _ABROAD_RX.search(part)
        if m:
            abroad.append(raw.strip())
        elif not (_STREET.search(part) and not re.search(r"\b\d{4}\b", part)):
            unclear.append(raw.strip())
    return {"towns": towns, "abroad": abroad, "unclear": unclear}


def reach(location: str | None, home: str | None, max_km: float | None = None) -> dict | None:
    """How far a job is from home. None when home or the job's place isn't known here.

    {"places": [(town, km)…] nearest first, "town", "km", "road_km" (rough), "abroad": […],
     "inside": True when a named town is within max_km, False when every named place is known and none is,
               None when there is no limit or part of the location is unclear}"""
    h = read(home)["towns"]
    found = read(location)
    if not h or not (found["towns"] or found["abroad"]):
        return None
    places = sorted(((_TOWNS[t][0], round(km_between(h[0], t))) for t in found["towns"]), key=lambda x: x[1])
    out = {"home": _TOWNS[h[0]][0], "places": places, "abroad": found["abroad"],
           "town": places[0][0] if places else None, "km": places[0][1] if places else None,
           "road_km": round(places[0][1] * 1.25 / 5) * 5 if places else None, "inside": None}
    if max_km:
        if places and places[0][1] <= max_km:
            out["inside"] = True
        elif not found["unclear"]:
            out["inside"] = False
    return out


def describe(r: dict | None, max_km: float | None = None) -> str:
    """One line for a model or a page: 'Aarhus is 40 km from Horsens in a straight line (roughly 50 km by road)'."""
    if not r:
        return ""
    bits = [f"{name} is {km} km from {r['home']}" for name, km in r["places"][:4]]
    text = "; ".join(bits)
    if bits:
        text += f" in a straight line (the nearest is roughly {r['road_km']} km by road)" if len(bits) > 1 else \
            f" in a straight line (roughly {r['road_km']} km by road)"
    if r["abroad"]:
        text += ("; " if text else "") + f"{', '.join(r['abroad'][:3])} is outside Denmark"
    if max_km and r["inside"] is not None:
        text += f". Limit {max_km:g} km: {'inside' if r['inside'] else 'outside'}"
    return text + "."
