"""vdirsyncer backend: sync local CalDAV collections into contract rows.

This is the no-Google alternative to the gws backend. It reuses the exact
same contract and writer as gws, so the widget cannot tell which source
produced the file.

Flow:
  1. Run `vdirsyncer sync` to refresh the local .ics mirror.
  2. Walk <root>/<collection>/<calendar>/*.ics and metadata files.
  3. Parse each event, expand recurring series within the window.
  4. Emit one contract row per local day the event covers.

Only stdlib plus icalendar and dateutil are needed. No gcloud, no gws, no
Google Cloud project, no OAuth consent screen.
"""

import hashlib
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import dateutil.rrule as rrule
from icalendar import Calendar as ICalendar

from . import contract
from .normalize import NO_TITLE, _covered_days, _https_only

DEFAULT_ROOT = Path.home() / ".local" / "share" / "calendars"
DEFAULT_SYNC_BIN = "vdirsyncer"

# Google Calendar legacy colour names -> hex. vdirsyncer stores the calendar
# metadata "color" as one of these names. Unknown names fall back to a stable
# hash of the calendar id below.
COLOR_NAMES = {
    "lavender": "#a4bdfc",
    "sage": "#7ae7bf",
    "grape": "#dbadff",
    "flamingo": "#ff887c",
    "banana": "#fbd75b",
    "tangerine": "#ffb878",
    "peacock": "#46d6db",
    "graphite": "#e1e1e1",
    "blueberry": "#5484ed",
    "basil": "#51b749",
    "tomato": "#ff3b30",
    "dark blue": "#476b9b",
    "dark green": "#519a51",
    "red": "#d50000",
    "green": "#008000",
    "blue": "#0000ff",
    "purple": "#800080",
    "cyan": "#00bcd4",
    "teal": "#00695c",
    "pink": "#e91e63",
    "orange": "#ff6d00",
    "yellow": "#fdd835",
    "brown": "#795548",
    "grey": "#9e9e9e",
}

# Deterministic fallback palette when the metadata colour name is unknown.
FALLBACK_PALETTE = [
    "#476b9b",
    "#519a51",
    "#a4bdfc",
    "#ff887c",
    "#fbd75b",
    "#46d6db",
    "#dbadff",
    "#e1e1e1",
]


class VdirError(Exception):
    """Raised when the vdirsyncer backend cannot do its job."""


def fallback_color(calendar_id):
    """A stable, per-calendar colour derived from the id."""
    digest = hashlib.sha256(calendar_id.encode()).digest()
    return FALLBACK_PALETTE[digest[0] % len(FALLBACK_PALETTE)]


def run_sync(sync_bin, root, quiet=True):
    """Run vdirsyncer sync. Raises VdirError when the binary is missing."""
    if not sync_bin:
        raise VdirError("no vdirsyncer binary configured")

    try:
        result = subprocess.run(
            [sync_bin, "sync"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError as error:
        raise VdirError(f"vdirsyncer not found: {sync_bin!r}") from error
    except subprocess.TimeoutExpired as error:
        raise VdirError("vdirsyncer sync timed out") from error

    # vdirsyncer exits non-zero on a failed sync, but the local mirror is
    # still usable. Leave the stale-but-valid rows in place rather than
    # failing the whole widget.
    if result.returncode != 0:
        print(
            f"warning: vdirsyncer sync exited {result.returncode}; "
            "using the existing mirror",
            file=__import__("sys").stderr,
        )
    return result.returncode


def discover_calendars(root):
    """Yield (collection, calendar_dir) for every calendar under root.

    Layout: <root>/<collection>/<calendar-id>/. Each calendar directory
    holds .ics files plus optional `color` and `displayname` metadata.
    """
    root = Path(root)
    if not root.is_dir():
        raise VdirError(f"calendars root not found: {root}")

    for collection in sorted(p for p in root.iterdir() if p.is_dir()):
        for calendar_dir in sorted(p for p in collection.iterdir() if p.is_dir()):
            yield collection.name, calendar_dir


def calendar_metadata(calendar_dir):
    """Return (calendar_id, display_name, colour_hex) for one calendar dir."""
    calendar_id = calendar_dir.name

    display_name = calendar_id
    displayname_file = calendar_dir / "displayname"
    if displayname_file.is_file():
        value = displayname_file.read_text().strip()
        if value:
            display_name = value

    colour = None
    color_file = calendar_dir / "color"
    if color_file.is_file():
        value = color_file.read_text().strip().lower()
        colour = COLOR_NAMES.get(value)

    return calendar_id, display_name, colour or fallback_color(calendar_id)


def _text(value):
    return str(value or "").strip()


def _event_start(ev):
    """Aware datetime for DTSTART; all-day events land at local midnight."""
    raw = ev.decoded("dtstart")
    if isinstance(raw, date) and not isinstance(raw, datetime):
        local = datetime.now().astimezone().tzinfo
        return datetime(raw.year, raw.month, raw.day, tzinfo=local), True
    if raw.tzinfo is None:
        raise ValueError("DTSTART has no timezone")
    return raw, False


def _event_end(ev, start_dt, all_day):
    """Aware datetime for DTEND, falling back to DTSTART+DURATION."""
    for key in ("dtend", "duration"):
        if key not in ev:
            continue
        try:
            raw = ev.decoded(key)
        except ValueError:
            continue
        if key == "duration":
            return start_dt + raw, all_day
        if isinstance(raw, date) and not isinstance(raw, datetime):
            local = datetime.now().astimezone().tzinfo
            return datetime(raw.year, raw.month, raw.day, tzinfo=local), True
        if raw.tzinfo is None:
            raise ValueError("DTEND has no timezone")
        return raw, all_day
    if all_day:
        return start_dt + timedelta(days=1), True
    return start_dt, all_day


def _exdates(ev):
    """Set of naive UTC datetimes excluded from a recurring series."""
    if "exdate" not in ev:
        return set()
    try:
        decoded = ev.decoded("exdate")
    except (ValueError, TypeError):
        return set()
    if not isinstance(decoded, list):
        decoded = [decoded]
    excluded = set()
    for item in decoded:
        if isinstance(item, datetime):
            excluded.add(item.astimezone(ZoneInfo("UTC")).replace(tzinfo=None))
        elif isinstance(item, date):
            excluded.add(datetime(item.year, item.month, item.day))
    return excluded


def _expand_occurrences(ev, window_start, window_end):
    """Yield (start_dt, end_dt, all_day) for every occurrence in the window.

    A single VEVENT with an RRULE is expanded with dateutil; a plain event
    is yielded once. EXDATEs are skipped. Overrides (RECURRENCE-ID events)
    are handled by the caller, which pairs them up by start instant.
    """
    start_dt, all_day = _event_start(ev)

    if "rrule" not in ev:
        end_dt, all_day = _event_end(ev, start_dt, all_day)
        # Skip events entirely outside the sync window.
        if end_dt < window_start or start_dt > window_end:
            return
        yield start_dt, end_dt, all_day
        return

    try:
        rule = ev.decoded("rrule")
        if isinstance(rule, list):
            rule = rule[0]
    except (KeyError, IndexError, ValueError, TypeError):
        rule = None
    if rule is None:
        end_dt, all_day = _event_end(ev, start_dt, all_day)
        yield start_dt, end_dt, all_day
        return

    duration = _duration(ev, start_dt, all_day)
    excluded = _exdates(ev)

    try:
        instances = rrule.rrulestr(
            rule.to_ical().decode(), dtstart=start_dt, cache=True
        ).between(
            window_start.replace(tzinfo=None) if window_start.tzinfo is None else window_start,
            window_end,
            inc=True,
        )
    except (ValueError, TypeError) as error:
        print(f"warning: could not expand recurrence: {error}", file=__import__("sys").stderr)
        return

    for instance in instances:
        key = instance.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        if key in excluded:
            continue
        yield instance, instance + duration, all_day


def _duration(ev, start_dt, all_day):
    """timedelta for one occurrence of the event."""
    try:
        end_dt, _ = _event_end(ev, start_dt, all_day)
        return end_dt - start_dt
    except (KeyError, ValueError):
        return timedelta(hours=1)


def _row_from_occurrence(ev, start_dt, end_dt, all_day, calendar, local_tz):
    """One contract row per local day covered by this occurrence."""
    start_local = start_dt.astimezone(local_tz)
    end_local = end_dt.astimezone(local_tz)

    title = _text(ev.get("summary")) or NO_TITLE
    location = _text(ev.get("location"))

    meeting_url = _https_only(_text(ev.get("X-GOOGLE-CONFERENCE")))

    event_url = ""
    for key in ("URL", "X-GOOGLE-CALENDAR-CS"):
        value = _https_only(_text(ev.get(key)))
        if value:
            event_url = value
            break

    status = _text(ev.get("status")).lower()
    if status == "cancelled":
        return []

    rows = []
    for day in _covered_days(start_local, end_local, all_day):
        rows.append(
            {
                "id": _text(ev.get("uid")),
                "calendarId": calendar["id"],
                "calendarName": calendar["name"],
                "color": calendar["color"],
                "dateKey": day.isoformat(),
                "start": start_local.isoformat(),
                "end": end_local.isoformat(),
                "allDay": all_day,
                "title": title,
                "location": location,
                "meetingUrl": meeting_url,
                "eventUrl": event_url,
            }
        )
    return rows


def _occurrence_key(ev, start_dt):
    """Identity of one occurrence, shared by its copies in several calendars."""
    uid = _text(ev.get("uid"))
    if not uid:
        return None
    start_utc = start_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return (uid, start_utc.isoformat())


def ics_rows(text, calendar, local_tz, window_start, window_end):
    """Parse one .ics file into contract rows."""
    try:
        parsed = ICalendar.from_ical(text)
    except ValueError as error:
        raise VdirError(f"could not parse calendar: {error}") from error

    events = [ev for ev in parsed.walk("VEVENT")]

    # Split base events from overrides (RECURRENCE-ID marks an exception to a
    # recurring series).
    bases = [ev for ev in events if "recurrence-id" not in ev]
    overrides = [ev for ev in events if "recurrence-id" in ev]

    rows = []
    for ev in bases:
        uid = _text(ev.get("uid"))
        override_map = {}
        for override in overrides:
            if _text(override.get("uid")) != uid:
                continue
            try:
                rec_start, _ = _event_start(override)
            except (KeyError, ValueError):
                continue
            override_map[rec_start] = override

        for start_dt, end_dt, all_day in _expand_occurrences(ev, window_start, window_end):
            effective = override_map.get(start_dt, ev)
            if _text(effective.get("status")).lower() == "cancelled":
                continue
            rows.extend(
                _row_from_occurrence(
                    effective, start_dt, end_dt, all_day, calendar, local_tz
                )
            )
    return rows


def build_rows(root, local_tz, window_start, window_end, seen=None):
    """Walk every calendar and return deduplicated contract rows."""
    seen = set() if seen is None else seen
    rows = []

    for collection, calendar_dir in discover_calendars(root):
        calendar_id, display_name, colour = calendar_metadata(calendar_dir)
        calendar = {"id": calendar_id, "name": display_name, "color": colour}

        for ics_file in sorted(calendar_dir.glob("*.ics")):
            try:
                text = ics_file.read_text(errors="replace")
            except OSError as error:
                print(f"warning: could not read {ics_file}: {error}", file=__import__("sys").stderr)
                continue

            try:
                file_rows = ics_rows(text, calendar, local_tz, window_start, window_end)
            except VdirError as error:
                print(f"warning: {ics_file}: {error}", file=__import__("sys").stderr)
                continue

            for row in file_rows:
                key = (row["id"], row["dateKey"], row["start"], row["title"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)

    return rows
