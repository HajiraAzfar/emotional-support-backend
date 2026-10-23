"""
The reflection report as a PDF (implementation plan, Phase B).

It is drawn from exactly what Insights shows for the same period — insights.build
is the only source — so the PDF can never say something the app does not.
Counts, never percentages; self-reported ratings shown as the user's own; and a
section with too little in it says so instead of drawing a shape out of
nothing. It is a record, not an assessment, and the first page says that
before anything else.

ponytail: generated on request, not stored. The plan's issued-report history
(a table of reports to reopen and delete) can sit on top of render() unchanged
when it is wanted.
"""
from datetime import date
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FONTS = Path(__file__).resolve().parent.parent / "content" / "fonts"

# The brand's ink, accent and washes (EmotionalSupportApp/src/theme.ts).
INK = colors.HexColor("#30263B")
SOFT = colors.HexColor("#54495F")
FAINT = colors.HexColor("#6F6680")
PRIMARY = colors.HexColor("#685895")
LINE = colors.HexColor("#E8DDF3")
WASH = colors.HexColor("#F6F1FA")

JOURNAL_NAMES = {
    "check_in": "Check-in",
    "savouring": "Saving Journal",
    "thought": "Thought Journal",
    "exposure": "Exposure Journal",
    "free_write": "Free write",
    "chat": "AI Chat",
}

# How many rows a counts table shows before it says how many more there are.
TOP_ROWS = 12

_registered = False


def _fonts():
    """Inter for text and Playfair for headings, as in the app, once per process."""
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont("Inter", str(FONTS / "Inter-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("Inter-SemiBold", str(FONTS / "Inter-SemiBold.ttf")))
        pdfmetrics.registerFont(TTFont("Playfair", str(FONTS / "PlayfairDisplay-Bold.ttf")))
        _registered = True


def _styles():
    return {
        "title": ParagraphStyle("title", fontName="Playfair", fontSize=24, leading=30, textColor=INK),
        "h2": ParagraphStyle("h2", fontName="Playfair", fontSize=15, leading=20, textColor=INK,
                             spaceBefore=16, spaceAfter=6),
        "body": ParagraphStyle("body", fontName="Inter", fontSize=10, leading=15, textColor=SOFT,
                               alignment=TA_LEFT),
        "small": ParagraphStyle("small", fontName="Inter", fontSize=8.5, leading=12, textColor=FAINT),
        "note": ParagraphStyle("note", fontName="Inter", fontSize=9.5, leading=14, textColor=INK,
                               backColor=WASH, borderPadding=(8, 10, 8, 10), spaceBefore=10,
                               spaceAfter=10),
        "cell": ParagraphStyle("cell", fontName="Inter", fontSize=9.5, leading=13, textColor=INK),
    }


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b %Y')}"


def _period(data: dict) -> str:
    if data.get("from"):
        return f"{_day(data['from'])} to {_day(data['to'])}"
    return f"All of your history, up to {_day(data['to'])}"


def _table(rows: list[list], s, widths, header: bool = True) -> Table:
    """Rows of text in the brand's type; the first row bold when it is a header.
    Every value is escaped, since names and fears are the user's own words."""
    def cell(value, bold: bool) -> Paragraph:
        text = escape(str(value))
        return Paragraph(f"<font name='Inter-SemiBold'>{text}</font>" if bold else text, s["cell"])

    cells = [[cell(value, header and i == 0) for value in row] for i, row in enumerate(rows)]
    table = Table(cells, colWidths=widths, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _counts(title: str, rows: list[dict], none: str, s, width: float) -> list:
    out = [Paragraph(title, s["h2"])]
    if not rows:
        return out + [Paragraph(none, s["body"])]
    shown = rows[:TOP_ROWS]
    body = [["", "Entries"]] + [[row["name"], row["count"]] for row in shown]
    out.append(_table(body, s, [width * 0.75, width * 0.25]))
    if len(rows) > TOP_ROWS:
        out.append(Paragraph(f"And {len(rows) - TOP_ROWS} more, each in fewer entries.", s["small"]))
    return out


def _mood_chart(points: list[dict], low: int, high: int, width: float) -> Drawing:
    """One dot per recorded mood, joined in order; no gaps are filled in."""
    first = date.fromisoformat(points[0]["date"])
    data = [((date.fromisoformat(p["date"]) - first).days, p["value"]) for p in points]
    span = max(1, data[-1][0])

    height = 150
    drawing = Drawing(width, height)
    plot = LinePlot()
    plot.x, plot.y = 28, 24
    plot.width, plot.height = width - 40, height - 40
    plot.data = [data]
    plot.lines[0].strokeColor = PRIMARY
    plot.lines[0].strokeWidth = 1.6
    plot.joinedLines = 1
    plot.lines[0].symbol = makeMarker("FilledCircle", size=4, fillColor=PRIMARY, strokeColor=PRIMARY)
    plot.xValueAxis.valueMin, plot.xValueAxis.valueMax = 0, span
    plot.xValueAxis.visibleLabels = 0
    plot.xValueAxis.strokeColor = LINE
    plot.yValueAxis.valueMin, plot.yValueAxis.valueMax = low, high
    plot.yValueAxis.valueStep = 1
    plot.yValueAxis.strokeColor = LINE
    plot.yValueAxis.labels.fontName = "Inter"
    plot.yValueAxis.labels.fontSize = 8
    plot.yValueAxis.labels.fillColor = FAINT
    plot.yValueAxis.visibleGrid = 1
    plot.yValueAxis.gridStrokeColor = LINE
    drawing.add(plot)
    for x, text, anchor in ((plot.x, _day(points[0]["date"]), "start"),
                            (plot.x + plot.width, _day(points[-1]["date"]), "end")):
        drawing.add(String(x, 8, text, fontName="Inter", fontSize=8, fillColor=FAINT, textAnchor=anchor))
    return drawing


def render(data: dict, *, name: str | None, made_on: date) -> bytes:
    """The PDF for one insights.build() result."""
    _fonts()
    s = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
        bottomMargin=18 * mm, title="Mind Doc reflection report", author="Mind Doc",
    )
    width = doc.width
    story: list = []

    story.append(Paragraph("Your reflection report", s["title"]))
    meta = [f"Mind Doc · {_period(data)}", f"Made on {made_on.day} {made_on.strftime('%b %Y')}"]
    if name:
        meta.insert(0, f"For {escape(name)}")
    story.append(Paragraph("<br/>".join(meta), s["small"]))
    story.append(Paragraph(
        "This is a record of what you wrote in Mind Doc, counted from the entries you finished "
        "in this period. It is not a clinical assessment, and nothing in it is a diagnosis. "
        "If you share it with someone who supports you, it shows what you recorded, in your own terms.",
        s["note"],
    ))

    # The period at a glance.
    days = len(data["calendar"]["days"])
    moods = len(data["mood"]["points"])
    story.append(Paragraph("At a glance", s["h2"]))
    story.append(_table([
        ["Finished entries", data["completed_entries"]],
        ["Days with at least one entry", days],
        ["Check-ins with a mood", moods],
    ], s, [width * 0.75, width * 0.25], header=False))

    story.append(Paragraph("Journals you used", s["h2"]))
    if data["journal_types"]:
        story.append(_table(
            [["", "Entries"]] + [[JOURNAL_NAMES.get(r["journal_type"], r["journal_type"]), r["count"]]
                                 for r in data["journal_types"]],
            s, [width * 0.75, width * 0.25]))
    else:
        story.append(Paragraph("No finished entries in this period.", s["body"]))

    # Mood, only once there is enough to be more than a dot or two.
    mood = data["mood"]
    block = [Paragraph("Mood", s["h2"])]
    if moods < data["min_points"]:
        more = mood["needed"]
        block.append(Paragraph(
            f"{more} more check-in{'s' if more != 1 else ''} with a mood will show how it moved over time.",
            s["body"]))
    else:
        block.append(Paragraph(f"Each dot is a mood you recorded ({mood['min']} lowest, {mood['max']} highest).",
                               s["small"]))
        block.append(_mood_chart(mood["points"], mood["min"], mood["max"], width))
    if any(row["count"] for row in data["mood_distribution"]):
        block.append(Spacer(1, 6))
        block.append(_table(
            [["Mood", "Entries"]] + [[r["label"], r["count"]] for r in data["mood_distribution"]],
            s, [width * 0.75, width * 0.25]))
    story.append(KeepTogether(block))

    story += _counts("Feelings you named", data["feelings"], "No feelings named in this period.", s, width)
    story += _counts("What you noted as triggers", data["triggers"], "No triggers noted in this period.", s, width)
    story += _counts("Thinking patterns you noticed", data["thinking_traps"],
                     "No thinking patterns noted in this period.", s, width)

    # The thought journal's own before-and-after ratings, as the user gave them.
    if data["rating_shifts"]:
        story.append(Paragraph("Feelings before and after the Thought Journal", s["h2"]))
        story.append(Paragraph(
            "Your own ratings (0 to 100), averaged over the entries where you rated a feeling both times.",
            s["small"]))
        story.append(_table(
            [["Feeling", "Before", "After", "Entries"]]
            + [[r["label"], r["before"], r["after"], r["entries"]] for r in data["rating_shifts"]],
            s, [width * 0.46, width * 0.18, width * 0.18, width * 0.18]))

    if data["exposure"]:
        story.append(Paragraph("Exposure, cycle by cycle", s["h2"]))
        story.append(Paragraph("The distress you recorded for each cycle, 0 to 10.", s["small"]))
        for group in data["exposure"]:
            rows = [["Cycle", "Date", "Before", "During", "After"]] + [
                [c["cycle"], _day(c["completed_at"]),
                 "" if c["before"] is None else c["before"],
                 "" if c["during"] is None else c["during"],
                 "" if c["after"] is None else c["after"]]
                for c in group["cycles"]
            ]
            story.append(KeepTogether([
                Spacer(1, 6),
                Paragraph(f"<font name='Inter-SemiBold'>{escape(group['feared_outcome'])}</font>", s["cell"]),
                _table(rows, s, [width * 0.12, width * 0.34, width * 0.18, width * 0.18, width * 0.18]),
            ]))

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont("Inter", 8)
        canvas.setFillColor(FAINT)
        canvas.drawString(document.leftMargin, 10 * mm, "Mind Doc · a record of your own entries")
        canvas.drawRightString(document.leftMargin + document.width, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
