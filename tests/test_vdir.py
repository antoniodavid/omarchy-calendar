import unittest
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from omarchy_calendar_sync import vdir

HAVANA = ZoneInfo("America/Havana")
UTC = timezone.utc

# Window: 7 days past -> 60 days future from a fixed "now".
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(days=7)
WINDOW_END = NOW + timedelta(days=60)

CAL = {"id": "work@example.com", "name": "Work", "color": "#476b9b"}

SIMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
UID:simple-1@example.com
DTSTART;TZID=America/Havana:20260815T090000
DTEND;TZID=America/Havana:20260815T094500
SUMMARY:Standup
LOCATION:Zoom
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""

RECURRING_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
UID:weekly-1@example.com
DTSTART;TZID=America/Havana:20260817T100000
DTEND;TZID=America/Havana:20260817T110000
RRULE:FREQ=WEEKLY;COUNT=4
SUMMARY:Weekly sync
STATUS:CONFIRMED
END:VEVENT
END:VCALENDAR
"""

CANCELLED_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
BEGIN:VEVENT
UID:cancelled-1@example.com
DTSTART;TZID=America/Havana:20260815T150000
DTEND;TZID=America/Havana:20260815T160000
SUMMARY:Old meeting
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


class TestIcsRows(unittest.TestCase):
    def test_simple_event_produces_one_row(self):
        rows = vdir.ics_rows(SIMPLE_ICS, CAL, HAVANA, WINDOW_START, WINDOW_END)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "simple-1@example.com")
        self.assertEqual(row["title"], "Standup")
        self.assertEqual(row["location"], "Zoom")
        self.assertEqual(row["dateKey"], "2026-08-15")
        self.assertEqual(row["allDay"], False)
        self.assertEqual(row["color"], "#476b9b")
        self.assertEqual(row["meetingUrl"], "")

    def test_event_outside_window_is_skipped(self):
        old = SIMPLE_ICS.replace("20260815", "20200101")
        rows = vdir.ics_rows(old, CAL, HAVANA, WINDOW_START, WINDOW_END)
        self.assertEqual(rows, [])

    def test_recurring_series_expands(self):
        rows = vdir.ics_rows(RECURRING_ICS, CAL, HAVANA, WINDOW_START, WINDOW_END)
        self.assertEqual(len(rows), 4)
        keys = {row["dateKey"] for row in rows}
        self.assertEqual(keys, {"2026-08-17", "2026-08-24", "2026-08-31", "2026-09-07"})

    def test_cancelled_event_is_dropped(self):
        rows = vdir.ics_rows(CANCELLED_ICS, CAL, HAVANA, WINDOW_START, WINDOW_END)
        self.assertEqual(rows, [])

    def test_multi_day_event_emits_one_row_per_day(self):
        ics = SIMPLE_ICS.replace(
            "DTSTART;TZID=America/Havana:20260815T090000",
            "DTSTART;VALUE=DATE:20260815",
        ).replace("DTEND;TZID=America/Havana:20260815T094500", "DTEND;VALUE=DATE:20260817")
        rows = vdir.ics_rows(ics, CAL, HAVANA, WINDOW_START, WINDOW_END)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["allDay"] for r in rows))

    def test_meeting_url_is_kept(self):
        ics = SIMPLE_ICS.replace(
            "SUMMARY:Standup",
            "SUMMARY:Standup\nX-GOOGLE-CONFERENCE:https://meet.google.com/abc-def-ghi",
        )
        rows = vdir.ics_rows(ics, CAL, HAVANA, WINDOW_START, WINDOW_END)
        self.assertEqual(rows[0]["meetingUrl"], "https://meet.google.com/abc-def-ghi")


class TestCalendarMetadata(unittest.TestCase):
    def test_fallback_color_is_stable_and_hex(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            cal_dir = Path(tmp) / "personal"
            cal_dir.mkdir()
            calendar_id, name, color = vdir.calendar_metadata(cal_dir)
            self.assertEqual(calendar_id, "personal")
            self.assertEqual(name, "personal")
            self.assertEqual(len(color), 7)
            self.assertEqual(color[0], "#")


if __name__ == "__main__":
    unittest.main()
