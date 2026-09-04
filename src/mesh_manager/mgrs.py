"""MGRS and UTM on WGS84 (Spec 023). Snyder's series as every field tool uses; the overlay's
JavaScript mirrors this function for function so the screen and the map agree to the metre."""
import math

A = 6378137.0
F = 1 / 298.257223563
K0 = 0.9996
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)
BANDS = "CDEFGHJKLMNPQRSTUVWX"
COLS = ("ABCDEFGH", "JKLMNPQR", "STUVWXYZ")
ROWS = "ABCDEFGHJKLMNPQRSTUV"


def zone_for(lat, lon):
    z = int(math.floor((lon + 180) / 6)) + 1
    if 56 <= lat < 64 and 3 <= lon < 12:
        z = 32
    if 72 <= lat < 84:
        if 0 <= lon < 9: z = 31
        elif 9 <= lon < 21: z = 33
        elif 21 <= lon < 33: z = 35
        elif 33 <= lon < 42: z = 37
    return z


def band_for(lat):
    if lat < -80 or lat > 84:
        return None
    i = min(19, int(math.floor((lat + 80) / 8)))
    return BANDS[i]


def to_utm(lat, lon, zone=None):
    """(zone, hemisphere, easting, northing)."""
    lat = float(lat); lon = float(lon)
    zone = zone or zone_for(lat, lon)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    p = math.radians(lat); l = math.radians(lon)
    n = A / math.sqrt(1 - E2 * math.sin(p) ** 2)
    t = math.tan(p) ** 2
    c = EP2 * math.cos(p) ** 2
    a = (l - lon0) * math.cos(p)
    m = A * ((1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256) * p
             - (3 * E2 / 8 + 3 * E2 ** 2 / 32 + 45 * E2 ** 3 / 1024) * math.sin(2 * p)
             + (15 * E2 ** 2 / 256 + 45 * E2 ** 3 / 1024) * math.sin(4 * p)
             - (35 * E2 ** 3 / 3072) * math.sin(6 * p))
    x = K0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5 / 120) + 500000.0
    y = K0 * (m + n * math.tan(p) * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                                       + (61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6 / 720))
    south = lat < 0
    if south:
        y += 10000000.0
    return zone, ("S" if south else "N"), x, y


def from_utm(zone, hemisphere, x, y):
    """(lat, lon) from a UTM position."""
    if hemisphere == "S":
        y -= 10000000.0
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    m = y / K0
    mu = m / (A * (1 - E2 / 4 - 3 * E2 ** 2 / 64 - 5 * E2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - E2)) / (1 + math.sqrt(1 - E2))
    p1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
          + (151 * e1 ** 3 / 96) * math.sin(6 * mu))
    n1 = A / math.sqrt(1 - E2 * math.sin(p1) ** 2)
    t1 = math.tan(p1) ** 2
    c1 = EP2 * math.cos(p1) ** 2
    r1 = A * (1 - E2) / (1 - E2 * math.sin(p1) ** 2) ** 1.5
    d = (x - 500000.0) / (n1 * K0)
    lat = p1 - (n1 * math.tan(p1) / r1) * (d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * EP2) * d ** 4 / 24
                                           + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * EP2 - 3 * c1 * c1) * d ** 6 / 720)
    lon = lon0 + (d - (1 + 2 * t1 + c1) * d ** 3 / 6 + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * EP2 + 24 * t1 * t1) * d ** 5 / 120) / math.cos(p1)
    return math.degrees(lat), math.degrees(lon)


def square(zone, x, y):
    """The 100 km square letters."""
    col = COLS[(zone - 1) % 3][int(math.floor(x / 100000)) - 1]
    row = ROWS[(int(math.floor(y / 100000)) + (5 if zone % 2 == 0 else 0)) % 20]
    return col + row


def mgrs(lat, lon, precision=5):
    """'30U XB 04250 74320' at 5 digits (metres); 4 gives tens of metres; 0 the square alone."""
    band = band_for(lat)
    if band is None:
        return None
    zone, hemi, x, y = to_utm(lat, lon)
    sq = square(zone, x, y)
    p = max(0, min(5, int(precision)))
    if p == 0:
        return f"{zone}{band} {sq}"
    div = 10 ** (5 - p)
    e = int(math.floor((x % 100000) / div))
    n = int(math.floor((y % 100000) / div))
    return f"{zone}{band} {sq} {e:0{p}d} {n:0{p}d}"
