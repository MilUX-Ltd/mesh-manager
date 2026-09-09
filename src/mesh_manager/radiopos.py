"""Spec 071: when the box should tell its radio where it is.

The box has a GPS receiver; the radio, half a metre away on the end of a USB cable, usually has
none. The receiver's fix is the radio's position for any purpose that matters, but every write to
a radio is a write to its flash, so this module answers one question and nothing else: is a push
due? The bridge decides what the position IS (own_position), and the firmware decides what to do
with it.
"""
import math

MIN_MOVE_M = 15.0     # below this it is receiver noise, not movement
MIN_SECS = 3600.0     # and a refresh once an hour, so a radio that was told long ago is not trusted forever

# Where a position must come from before it is worth broadcasting. `devices` is the median of the
# fixes a box HEARS: an estimate of where it is, derived from everyone else. Pushing that to the
# radio would broadcast a guess as a fact, and that guess then feeds back into everyone else's
# picture of where things are. `radio_stored` is whatever a radio was last told by anyone, which is
# not evidence of anything.
TRUSTED_SOURCES = ("gps", "radio_gps", "declared", "config")


def worth_broadcasting(source):
    """True when a position is a fix or a deliberate declaration, not an estimate."""
    return str(source or "") in TRUSTED_SOURCES


def metres_between(lat1, lon1, lat2, lon2):
    """Great-circle distance. Good to well under a metre at the scale that matters here."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def due(now, current, last, min_move_m=MIN_MOVE_M, min_secs=MIN_SECS):
    """(due, why). `current` is (lat, lon); `last` is what was pushed, or None.

    The reason is carried so the screen and the log can say why a radio was written to, rather than
    leaving an operator to guess why their flash is being touched.
    """
    if current is None:
        return False, "the box has no position"
    if not last or last.get("lat") is None or last.get("lon") is None:
        return True, "the radio has not been told where it is"
    moved = metres_between(last["lat"], last["lon"], current[0], current[1])
    if moved > min_move_m:
        return True, f"the box has moved {moved:.0f} m"
    age = now - float(last.get("at") or 0)
    if age >= min_secs:
        return True, f"the radio was last told {int(age / 60)} minutes ago"
    return False, f"unchanged ({moved:.1f} m) and told {int(age / 60)} minutes ago"
