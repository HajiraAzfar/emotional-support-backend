"""
The check-in reports as PDFs: a day, a week or a month, drawn from exactly
what checkin_reports.build() returns.

Conclusions come first, then the charts and tables they were read from, then
her own words, then where to find support. A section with nothing in it is
left out, and a chart is drawn only when it has something to show. The
brand's fonts, colours and the escaping of her words come from pdf_report.
"""
import json
import math
from datetime import date
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, Rect, String
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer

from app.core import checkin_reports as reports
from app.core.pdf_report import FAINT, INK, LINE, PRIMARY, SOFT, _fonts, _styles, _table

HELPLINES = Path(__file__).resolve().parent.parent / "config" / "helplines.json"

SLOT_NAMES = {"morning": "Morning", "afternoon": "Afternoon", "evening": "Evening"}
SLOT_COLOURS = {"morning": colors.HexColor("#D9A05B"), "afternoon": colors.HexColor("#5E7A57"), "evening": PRIMARY}
ROSE = colors.HexColor("#D06A86")
LEAF = colors.HexColor("#5E7A57")
# Very low to very good, in the brand's lavender and rose: deeper at either end,
# pale at "okay", so a calendar reads as a scale on paper. (The app's own mood
# colours sit beside faces and are too close in lightness for that.)
MOOD_COLOURS = [colors.HexColor(c) for c in ("#9A88CC", "#C9BDE9", "#E2D9EC", "#F5C6D0", "#E393A6")]
MOOD_WORDS = ["Very low", "Low", "Okay", "Good", "Very good"]
# How many rows a counts table shows.
TOP_ROWS = 8
TITLES = {reports.DAILY: "Your day", reports.WEEKLY: "Your week", reports.MONTHLY: "Your month"}


# ---------- charts ----------

def _value_axis(axis, low: float, high: float, step: float, labels: bool = True):
    axis.valueMin, axis.valueMax, axis.valueStep = low, high, step
    axis.strokeColor = LINE
    axis.visibleLabels = 1 if labels else 0
    axis.labels.fontName = "Inter"
    axis.labels.fontSize = 8
    axis.labels.fillColor = FAINT
    axis.visibleGrid = 1
    axis.gridStrokeColor = LINE


def _legend(drawing: Drawing, items: list[tuple[str, colors.Color]], y: float):
    x = 28
    for name, colour in items:
        drawing.add(Rect(x, y, 8, 8, fillColor=colour, strokeColor=None))
        drawing.add(String(x + 12, y + 1, name, fontName="Inter", fontSize=8, fillColor=SOFT))
        x += 30 + len(name) * 4.5


def _bars(categories: list[str], series: list[tuple[str, list, colors.Color]], low: float, high: float,
          step: float, width: float, height: float = 140) -> Drawing | None:
    """Grouped bars, one colour per series; a missing value is no bar, never a zero."""
    series = [s for s in series if any(v is not None for v in s[1])]
    if not series:
        return None
    legend = len(series) > 1
    drawing = Drawing(width, height + (14 if legend else 0))
    chart = VerticalBarChart()
    chart.x, chart.y = 28, 18
    chart.width, chart.height = width - 40, height - 28
    chart.data = [values for _, values, _ in series]
    for i, (_, _, colour) in enumerate(series):
        chart.bars[i].fillColor = colour
        chart.bars[i].strokeColor = None
    chart.barSpacing = 2
    chart.groupSpacing = 8
    chart.categoryAxis.categoryNames = categories
    chart.categoryAxis.strokeColor = LINE
    chart.categoryAxis.labels.fontName = "Inter"
    chart.categoryAxis.labels.fontSize = 8
    chart.categoryAxis.labels.fillColor = FAINT
    _value_axis(chart.valueAxis, low, high, step)
    drawing.add(chart)
    if legend:
        _legend(drawing, [(name, colour) for name, _, colour in series], height + 2)
    return drawing


def _lines(labels: list[str], series: list[tuple[str, list, colors.Color]], low: float, high: float,
           step: float, width: float, height: float = 150, y_labels: bool = True) -> Drawing | None:
    """One line per series over the labels; a gap is a gap, never filled in."""
    drawn = [(name, [(i, v) for i, v in enumerate(values) if v is not None], colour)
             for name, values, colour in series]
    drawn = [d for d in drawn if d[1]]
    if not drawn:
        return None
    legend = len(series) > 1
    drawing = Drawing(width, height + (14 if legend else 0))
    plot = LinePlot()
    plot.x, plot.y = 28, 18
    plot.width, plot.height = width - 40, height - 28
    plot.data = [points for _, points, _ in drawn]
    plot.joinedLines = 1
    for i, (_, _, colour) in enumerate(drawn):
        plot.lines[i].strokeColor = colour
        plot.lines[i].strokeWidth = 1.6
        plot.lines[i].symbol = makeMarker("FilledCircle", size=4, fillColor=colour, strokeColor=colour)
    # Half a step of room at each end, so the first and last labels stay on the page.
    plot.xValueAxis.valueMin, plot.xValueAxis.valueMax = -0.5, len(labels) - 0.5
    plot.xValueAxis.visibleLabels = 0
    plot.xValueAxis.strokeColor = LINE
    _value_axis(plot.yValueAxis, low, high, step, labels=y_labels)
    drawing.add(plot)
    for i, label in enumerate(labels):
        drawing.add(String(plot.x + plot.width * (i + 0.5) / len(labels), 5, label, fontName="Inter", fontSize=8,
                           fillColor=FAINT, textAnchor="middle"))
    if legend:
        _legend(drawing, [(name, colour) for name, _, colour in drawn], height + 2)
    return drawing


def _calendar(month_start: date, moods: dict[str, float | None], width: float) -> Drawing:
    """The month as a calendar, each day tinted by its average mood; a day without one stays blank."""
    days_in_month = (date(month_start.year + month_start.month // 12, month_start.month % 12 + 1, 1)
                     - month_start).days
    offset = month_start.weekday()
    weeks = math.ceil((offset + days_in_month) / 7)
    cell, gap = min(36.0, (width - 6 * 4) / 7), 4
    height = 16 + weeks * (cell + gap) + 22
    drawing = Drawing(width, height)
    top = height - 12
    for col, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
        drawing.add(String(col * (cell + gap) + cell / 2, top, name, fontName="Inter", fontSize=8,
                           fillColor=FAINT, textAnchor="middle"))
    for n in range(days_in_month):
        position = offset + n
        col, row = position % 7, position // 7
        x = col * (cell + gap)
        y = top - 8 - (row + 1) * (cell + gap) + gap
        mood = moods.get(date(month_start.year, month_start.month, n + 1).isoformat())
        fill = MOOD_COLOURS[min(4, max(0, round(mood) - 1))] if mood is not None else colors.white
        drawing.add(Rect(x, y, cell, cell, rx=5, ry=5, fillColor=fill, strokeColor=LINE, strokeWidth=0.6))
        drawing.add(String(x + cell / 2, y + cell / 2 - 3, str(n + 1), fontName="Inter", fontSize=8,
                           fillColor=INK, textAnchor="middle"))
    x = 0
    for colour, word in zip(MOOD_COLOURS, MOOD_WORDS):
        drawing.add(Rect(x, 2, 9, 9, rx=2, ry=2, fillColor=colour, strokeColor=None))
        drawing.add(String(x + 13, 3, word, fontName="Inter", fontSize=8, fillColor=SOFT))
        x += 26 + len(word) * 4.5
    return drawing


# ---------- pieces of text ----------

def _bullet_style(s) -> ParagraphStyle:
    return ParagraphStyle("bullet", parent=s["body"], leftIndent=12, bulletIndent=0, spaceAfter=3, textColor=INK)


def _section(title: str, body: list, s, note: str | None = None) -> list:
    """A heading (and its note) kept with what follows; nothing at all when there is nothing to show."""
    body = [b for b in body if b is not None]
    if not body:
        return []
    head = [Paragraph(title, s["h2"])] + ([Paragraph(note, s["small"])] if note else [])
    return [KeepTogether(head + body)]


def _pairs(rows: list[tuple[str, object]], s, width: float):
    """Label and value, for the rows that have a value."""
    rows = [[label, value] for label, value in rows if value not in (None, "", [])]
    return _table(rows, s, [width * 0.34, width * 0.66], header=False) if rows else None


def _out_of(value, scale: int) -> str | None:
    return None if value is None else f"{value}/{scale}"


def _counts(rows: list[dict], unit: str, key: str, s, width: float):
    if not rows:
        return None
    shown = [[row["name"], row[key]] for row in rows[:TOP_ROWS]]
    return _table([["", unit]] + shown, s, [width * 0.75, width * 0.25])


def _short_day(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%a")


def _day_month(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def _words(report: dict, s, width: float) -> list:
    out = []
    for key, heading in (("wins", "Your wins"), ("helped", "What helped"), ("hard", "What was hard")):
        rows = report["words"][key]
        if rows:
            out.append(Paragraph(f"<font name='Inter-SemiBold'>{heading}</font>", s["cell"]))
            out.append(_table([[_day_month(r["date"]), r["text"]] for r in rows], s,
                              [width * 0.16, width * 0.84], header=False))
            out.append(Spacer(1, 6))
    return out


def _questions(report: dict, s, width: float) -> list:
    """PHQ-9 and GAD-7 as unlabelled lines: positions only, no score, no band, no condition named."""
    phq, gad = report["questions"]["phq9"], report["questions"]["gad7"]
    if not phq and not gad:
        return []
    dates = sorted({p["date"] for p in phq + gad})
    if len(dates) < 2:
        return _section("Your weekly questions", [Paragraph(
            "One week of answers so far; a second will draw a line showing how they move.", s["body"])], s)
    at = {d: i for i, d in enumerate(dates)}

    def values(points):
        row = [None] * len(dates)
        for p in points:
            row[at[p["date"]]] = p["position"]
        return row

    chart = _lines([_day_month(d) for d in dates],
                   [("First set (9 questions)", values(phq), PRIMARY), ("Second set (7 questions)", values(gad), ROSE)],
                   0, 1, 0.25, width, height=120, y_labels=False)
    return _section("Your weekly questions", [chart], s, note=escape(reports.TEXT["questions_note"]))


def _support(s, width: float) -> list:
    with open(HELPLINES, encoding="utf-8") as f:
        lines = json.load(f)["resources"]
    return [KeepTogether([
        Paragraph(escape(reports.TEXT["support_title"]), s["h2"]),
        _table([[line["name"], line["phone"]] for line in lines], s, [width * 0.7, width * 0.3], header=False),
        Spacer(1, 8),
        Paragraph(escape(reports.TEXT["closing"]), s["small"]),
    ])]


# ---------- one day ----------

def _daily(report: dict, s, width: float) -> list:
    rows = report["slots"]

    def cells(fn):
        return [fn(r) if r["done"] else "—" for r in rows]

    def rated(scale: str, value) -> str:
        return "—" if value is None else f"{reports._label(scale, value)} ({value}/5)"

    glance = _table([
        [""] + [SLOT_NAMES[r["slot"]] for r in rows],
        ["Check-in"] + ["Done" if r["done"] else "Not done" for r in rows],
        ["Mood"] + cells(lambda r: rated("mood_5", r["mood"])),
        ["Stress or worry"] + cells(lambda r: _out_of(r["stress"], 10) or "—"),
        ["Energy"] + cells(lambda r: rated("energy_5", r["energy"])),
    ], s, [width * 0.28] + [width * 0.24] * 3)

    names = [SLOT_NAMES[r["slot"]] for r in rows]
    mood_energy = _bars(names, [("Mood", [r.get("mood") for r in rows], PRIMARY),
                                ("Energy", [r.get("energy") for r in rows], LEAF)], 0, 5, 1, width, height=120)
    stress = _bars(names, [("Stress or worry", [r.get("stress") for r in rows], ROSE)], 0, 10, 2, width, height=110)

    felt = [[SLOT_NAMES[r["slot"]], ", ".join(f"{f['name']} ({f['strength']}/10)" if f["strength"] is not None
                                                else f["name"] for f in r["feelings"])]
            for r in rows if r["done"] and r["feelings"]]

    out = _section("At a glance", [glance], s)
    out += _section("Mood and energy, through the day", [mood_energy], s)
    out += _section("Stress or worry, through the day", [stress], s)
    if felt:
        out += _section("Feelings you named", [_table(felt, s, [width * 0.22, width * 0.78], header=False)], s)

    morning, afternoon, evening = report["morning"], report["afternoon"], report["evening"]
    if morning:
        sleep = ", ".join(x for x in (morning["sleep_hours"],
                                      f"slept {morning['sleep_quality'].lower()}" if morning["sleep_quality"] else None)
                          if x)
        out += _section("Morning", [_pairs([
            ("Sleep", sleep),
            ("Intention", morning["intention"]),
            ("Hard about the day ahead", ", ".join(morning["hard_ahead"])),
            ("About it", morning["hard_note"]),
        ], s, width)], s)
    if afternoon:
        stressful = None
        if afternoon["stress_event"] is not None:
            stressful = "Yes" if afternoon["stress_event"] == 1 else "No"
            if afternoon["stress_about"]:
                stressful += f": {', '.join(afternoon['stress_about'])}"
        out += _section("Afternoon", [_pairs([
            ("The day so far", ", ".join(afternoon["activities"])),
            ("Enjoyed", _out_of(afternoon["pleasure"], 10)),
            ("Got done that mattered", _out_of(afternoon["achievement"], 10)),
            ("Something stressful", stressful),
            ("What happened", afternoon["stress_note"]),
            ("To cope", ", ".join(afternoon["coping"])),
        ], s, width)], s)
    if evening:
        day = None
        if evening["day_rating"] is not None:
            day = f"{evening['day_rating']}/10, {evening['day_rating_label'].lower()}"
        out += _section("Evening", [_pairs([
            ("The day overall", day),
            ("The day included", ", ".join(evening["activities"])),
            ("Went as hoped", evening["plan_label"]),
            ("What helped", evening["helped"]),
            ("What was hard", evening["hard"]),
            ("A win", evening["win"]),
            ("A thought still bothering you", evening["bothering_thoughts"]),
            ("A good moment to hold on to", evening["good_moment"]),
            ("Faced something you usually avoid", evening["faced_avoidance"]),
        ], s, width)], s)
    return out


# ---------- a week or a month ----------

def _glance(report: dict, s, width: float):
    g = report["glance"]
    rows = [["Check-ins", f"{g['check_ins']} of {g['possible']}"],
            ["Days with a check-in", f"{g['days_with']} of {g['days']}"]]
    rows += [[f"{SLOT_NAMES[slot]} check-ins", f"{n} of {g['days']}"] for slot, n in g["by_slot"].items()]
    return _table(rows, s, [width * 0.6, width * 0.4], header=False)


def _by_slot(report: dict, s, width: float):
    def show(value, scale):
        return "—" if value is None else f"{value}/{scale}"

    rows = [["", "Check-ins", "Mood", "Stress or worry", "Energy"]] + [
        [SLOT_NAMES[r["slot"]], r["check_ins"], show(r["mood"], 5), show(r["stress"], 10), show(r["energy"], 5)]
        for r in report["by_slot"]]
    return _table(rows, s, [width * 0.24, width * 0.16, width * 0.18, width * 0.24, width * 0.18])


def _sleep_mood(report: dict, s, width: float):
    sm = report["sleep_mood"]
    if not sm["short_days"] and not sm["enough_days"]:
        return None
    rows = [["Nights", "Days", "Mood that day"]]
    for label, mood, n in (("Under 6 hours", sm["short"], sm["short_days"]),
                           ("6 hours or more", sm["enough"], sm["enough_days"])):
        rows.append([label, n, "—" if mood is None else f"{mood}/5"])
    return _table(rows, s, [width * 0.4, width * 0.2, width * 0.4])


def _better_harder(report: dict, s, width: float):
    rows = []
    for label, group in (("Better days (rated 7 or more)", report["better_days"]),
                         ("Harder days (rated 4 or less)", report["harder_days"])):
        if group["days"]:
            held = ", ".join(f"{a['name']} ({a['days']})" for a in group["activities"]) or "Nothing noted"
            rows.append([f"{label}: {group['days']}", held])
    return _table(rows, s, [width * 0.38, width * 0.62], header=False) if rows else None


def _plan(report: dict, s, width: float):
    p = report["plan"]
    if not p["yes"] + p["partly"] + p["not_really"]:
        return None
    return _table([["Yes", p["yes"]], ["Partly", p["partly"]], ["Not really", p["not_really"]]],
                  s, [width * 0.75, width * 0.25], header=False)


def _period(report: dict, s, width: float) -> list:
    weekly = report["kind"] == reports.WEEKLY
    series = report["series"]
    out = _section("At a glance", [_glance(report, s, width)], s)

    if weekly:
        days = [_short_day(row["date"]) for row in series]
        out += _section("Mood through the week", [
            _lines(days, [(SLOT_NAMES[slot], [row["mood"][slot] for row in series], SLOT_COLOURS[slot])
                          for slot in SLOT_NAMES], 1, 5, 1, width)],
            s, note="Each line is one time of day, 1 (very low) to 5 (very good).")
        out += _section("Stress or worry, day by day", [
            _bars(days, [("Stress or worry", [row["stress"] for row in series], ROSE)], 0, 10, 2, width)], s)
    else:
        month_start = date.fromisoformat(report["start"])
        out += _section("Mood across the month", [
            _calendar(month_start, {row["date"]: row["day_mood"] for row in series}, width)],
            s, note="Each day is tinted by its average mood; a day without a check-in stays blank.")
        weeks = report["weeks"]
        labels = []
        for w in weeks:
            first, last = date.fromisoformat(w["start"]), date.fromisoformat(w["end"])
            labels.append(f"{first.day}–{last.day} {last.strftime('%b')}")
        out += _section("Mood, week by week", [
            _lines(labels, [("Mood", [w["mood"] for w in weeks], PRIMARY)], 1, 5, 1, width, height=120)], s)
        out += _section("Stress or worry, week by week", [
            _bars(labels, [("Stress or worry", [w["stress"] for w in weeks], ROSE)], 0, 10, 2, width, height=120)], s)

    out += _section("By time of day", [_by_slot(report, s, width)],
                    s, note="Averages of your own ratings at each time of day.")

    if weekly:
        out += _section("Enjoyment and getting things done", [
            _bars(days, [("Enjoyed", [row["pleasure"] for row in series], PRIMARY),
                         ("Got done", [row["achievement"] for row in series], LEAF)], 0, 10, 2, width)],
            s, note="From the afternoon check-ins, 0 to 10.")
        out += _section("Sleep", [
            _bars(days, [("Hours", [row["sleep_hours"] for row in series], SLOT_COLOURS["morning"])],
                  0, 10, 2, width, height=110),
            _sleep_mood(report, s, width)],
            s, note="Hours slept, from the morning check-ins (3 is under 4 hours, 10 is 10 or more).")
        out += _section("How the days felt", [
            _bars(days, [("The day overall", [row["day_rating"] for row in series], LEAF)], 0, 10, 2, width,
                  height=110)], s, note="Your evening rating of each day, 0 to 10.")
    else:
        out += _section("Sleep and mood", [_sleep_mood(report, s, width)], s)

    out += _section("Feelings you named", [_counts(report["feelings"], "Check-ins", "count", s, width)], s)
    out += _section("What felt hard or stressful", [_counts(report["triggers"], "Check-ins", "count", s, width)], s)
    out += _section("What the days held", [_counts(report["activities"], "Days", "days", s, width)], s)
    out += _section("What you did to cope", [_counts(report["coping"], "Times", "count", s, width)], s)
    out += _section("Better and harder days", [_better_harder(report, s, width)], s)
    out += _section("Did the day go as you hoped?", [_plan(report, s, width)], s)
    out += _section("In your own words", _words(report, s, width), s)
    out += _questions(report, s, width)
    return out


# ---------- the document ----------

def render(report: dict, *, name: str | None, made_on: date) -> bytes:
    _fonts()
    s = _styles()
    bullet = _bullet_style(s)
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
        bottomMargin=18 * mm, title=f"Mind Doc check-in report, {report['title']}", author="Mind Doc",
    )
    width = doc.width

    story: list = [Paragraph(TITLES[report["kind"]], s["title"])]
    meta = [f"Check-in report · {escape(report['title'])}", f"Made on {made_on.day} {made_on.strftime('%b %Y')}"]
    if name:
        meta.insert(0, f"For {escape(name)}")
    story.append(Paragraph("<br/>".join(meta), s["small"]))
    story.append(Paragraph(escape(reports.TEXT["note"].format(period=report["period"])), s["note"]))

    story.append(Paragraph("What stood out", s["h2"]))
    story += [Paragraph(escape(line), bullet, bulletText="•") for line in report["conclusions"]]

    story += _daily(report, s, width) if report["kind"] == reports.DAILY else _period(report, s, width)
    story += _support(s, width)

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Inter", 8)
        canvas.setFillColor(FAINT)
        canvas.drawString(document.leftMargin, 10 * mm, "Mind Doc · a record of your own check-ins")
        canvas.drawRightString(document.leftMargin + document.width, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
