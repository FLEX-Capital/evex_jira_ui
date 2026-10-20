"""Build the board-meeting deck for the Amparex AI-assistability analysis.

    uv run --group analysis --env-file .env python build_board_deck.py

Produces ``amparex_ai_leverage_board.pptx``: ten 16:9 slides meant to be dragged
into an existing board deck. Every chart is drawn from native PowerPoint shapes
rather than an exported image, so the figures stay crisp when projected and
anyone can restyle, recolour or delete a bar in PowerPoint without coming back
here.

The numbers are written out as literals so the deck rebuilds identically without
a Jira or Azure round trip. They come from ``amparex_ai_assist_analysis.ipynb``
over ``data/sdax_classifications.parquet``; regenerate them from the notebook if
the classification is ever re-run.
"""

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

OUTPUT = "amparex_ai_leverage_board.pptx"

# --- palette ---------------------------------------------------------------
# Resolution buckets and the AI-capability ramp, carried over from the findings
# page where they were checked for colour-vision separation. "unclear" is
# deliberately near-neutral: it is the absence of a classification, not a fourth
# category, so it never competes with the three real buckets.
INK = RGBColor(0x15, 0x1E, 0x1D)
INK_2 = RGBColor(0x3D, 0x4A, 0x48)
MUTED = RGBColor(0x6B, 0x78, 0x76)
LINE = RGBColor(0xDD, 0xE3, 0xE1)
SURFACE = RGBColor(0xF4, 0xF6, 0xF5)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

QA = RGBColor(0x00, 0x8A, 0x7E)
TODO = RGBColor(0xAE, 0x70, 0x00)
HANDOFF = RGBColor(0x70, 0x48, 0xA0)
UNCLEAR = RGBColor(0x6F, 0x81, 0x7D)

# Single-hue sequential ramp for the ordered capability scale.
R1 = RGBColor(0x0A, 0x4F, 0x4A)
R2 = RGBColor(0x12, 0x85, 0x7A)
R3 = RGBColor(0x57, 0xAD, 0xA3)
R4 = RGBColor(0xA8, 0xC8, 0xC4)

# Matrix heat steps, light to dark.
M = [
    RGBColor(0xF4, 0xF6, 0xF5),
    RGBColor(0xDF, 0xEA, 0xE8),
    RGBColor(0xA8, 0xC8, 0xC4),
    RGBColor(0x57, 0xAD, 0xA3),
    RGBColor(0x0A, 0x4F, 0x4A),
]

# Arial is present on every Windows and macOS machine a board deck might open
# on. A more characterful pairing risks silent substitution mid-presentation.
FONT = "Arial"

# --- geometry --------------------------------------------------------------
W, H = Inches(13.333), Inches(7.5)
PAD = Inches(0.85)
CONTENT_W = W - 2 * PAD
TOP = Inches(0.72)


def de(value):
    """German thousands separator, matching the rest of the analysis."""
    return f"{value:,}".replace(",", ".")


# --- primitives ------------------------------------------------------------


def text(
    slide,
    body,
    left,
    top,
    width,
    height,
    size=14,
    color=INK,
    bold=False,
    align=PP_ALIGN.LEFT,
    spacing=0.0,
    caps=False,
    line=1.2,
    anchor=MSO_ANCHOR.TOP,
):
    """Place one text box. ``body`` is a string, or a list of paragraphs."""
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0

    lines = [body] if isinstance(body, str) else list(body)
    for index, content in enumerate(lines):
        para = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        para.alignment = align
        para.line_spacing = line
        if index:
            para.space_before = Pt(6)
        run = para.add_run()
        run.text = content.upper() if caps else content
        font = run.font
        font.name = FONT
        font.size = Pt(size)
        font.bold = bold
        font.color.rgb = color
        if spacing:
            # python-pptx exposes no letter-spacing API; set it on the run's rPr.
            run._r.get_or_add_rPr().set("spc", str(int(spacing * 100)))
    return box


def rect(slide, left, top, width, height, fill):
    """A flat rectangle: no outline, no shadow, no theme inheritance."""
    shape = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE, int(left), int(top), max(int(width), 1), int(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = fill
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def eyebrow(slide, label):
    text(
        slide,
        label,
        PAD,
        TOP,
        CONTENT_W,
        Inches(0.25),
        size=10.5,
        color=MUTED,
        bold=True,
        spacing=1.6,
        caps=True,
    )


def heading(slide, title, sub=None):
    text(
        slide,
        title,
        PAD,
        TOP + Inches(0.3),
        CONTENT_W,
        Inches(0.62),
        size=27,
        color=INK,
        bold=True,
    )
    if sub:
        text(
            slide,
            sub,
            PAD,
            TOP + Inches(0.98),
            Inches(10.4),
            Inches(0.8),
            size=13,
            color=INK_2,
            line=1.35,
        )


def source(slide, note):
    text(
        slide, note, PAD, H - Inches(0.6), CONTENT_W, Inches(0.3), size=9.5, color=MUTED
    )


def bar_rows(slide, rows, top, *, label_w, bar_w, max_value, row_h, gap):
    """Horizontal bars on one shared scale, with a label and value per row.

    ``rows`` is a sequence of (label, sublabel, value, colour, value_text).
    Returns the x of the value column so callers can place a second figure.
    """
    bar_left = PAD + label_w + Inches(0.2)
    value_left = bar_left + bar_w + Inches(0.18)
    for index, (label, sublabel, value, colour, value_text) in enumerate(rows):
        y = top + index * (row_h + gap)
        text(
            slide,
            label,
            PAD,
            y + Inches(0.02),
            label_w,
            Inches(0.26),
            size=12,
            color=INK,
        )
        if sublabel:
            text(
                slide,
                sublabel,
                PAD,
                y + Inches(0.24),
                label_w,
                Inches(0.2),
                size=9.5,
                color=MUTED,
            )
        rect(slide, bar_left, y, bar_w, row_h - Inches(0.08), SURFACE)
        rect(
            slide,
            bar_left,
            y,
            bar_w * (value / max_value),
            row_h - Inches(0.08),
            colour,
        )
        text(
            slide,
            value_text,
            value_left,
            y + Inches(0.02),
            Inches(1.15),
            Inches(0.26),
            size=12,
            color=INK,
            bold=True,
        )
    return value_left


# --- slides ----------------------------------------------------------------


def slide_title(slide):
    rect(slide, 0, 0, W, Inches(0.11), R1)

    text(
        slide,
        "Amparex Service Desk · Jira SDAX",
        PAD,
        Inches(2.15),
        CONTENT_W,
        Inches(0.3),
        size=11.5,
        color=MUTED,
        bold=True,
        spacing=1.6,
        caps=True,
    )
    text(
        slide,
        "Half of Amparex support is work AI can carry",
        PAD,
        Inches(2.6),
        Inches(10.6),
        Inches(1.7),
        size=42,
        color=INK,
        bold=True,
        line=1.08,
    )
    text(
        slide,
        "52.694 tickets from 21 months, each classified by how it was actually "
        "resolved and how much of it AI could take over.",
        PAD,
        Inches(4.5),
        Inches(9.4),
        Inches(0.8),
        size=15,
        color=INK_2,
        line=1.4,
    )

    y = Inches(5.75)
    rect(slide, PAD, y, CONTENT_W, Emu(9525), LINE)
    facts = [
        ("Tickets classified", "52.694"),
        ("Period", "01.01.2025 – 16.09.2026"),
        ("Categories", "23"),
        ("AI-addressable", "51,8 %"),
    ]
    col = CONTENT_W / len(facts)
    for index, (key, value) in enumerate(facts):
        left = PAD + index * col
        text(
            slide,
            key,
            left,
            y + Inches(0.22),
            col,
            Inches(0.22),
            size=9.5,
            color=MUTED,
            bold=True,
            spacing=1.2,
            caps=True,
        )
        text(
            slide,
            value,
            left,
            y + Inches(0.5),
            col,
            Inches(0.34),
            size=16,
            color=INK,
            bold=True,
        )


def slide_headline(slide):
    eyebrow(slide, "The finding")
    heading(slide, "Half of the ticket volume is within reach of AI")

    text(
        slide,
        "51,8 %",
        PAD,
        Inches(1.8),
        Inches(4.0),
        Inches(1.5),
        size=76,
        color=INK,
        bold=True,
        line=0.95,
    )
    text(
        slide,
        [
            (
                "27.270 tickets AI could resolve outright, or draft completely for a "
                "human to approve."
            ),
            "Only 5,4 % needs a human from start to finish.",
        ],
        PAD + Inches(4.3),
        Inches(1.95),
        Inches(6.9),
        Inches(1.3),
        size=15,
        color=INK_2,
        line=1.45,
    )

    # One stacked bar across the full content width: the whole support volume,
    # ordered most to least automatable so it reads as a single scale.
    top = Inches(3.6)
    height = Inches(0.86)
    gap = Inches(0.022)
    segments = [
        (18.8, R1, "18,8 %", WHITE, "fully_automatable", "9.892 tickets"),
        (33.0, R2, "33,0 %", WHITE, "ai_drafts_human_sends", "17.378 tickets"),
        (42.8, R3, "42,8 %", INK, "ai_assisted_triage", "22.575 tickets"),
        (5.4, R4, "5,4 %", INK, "human_only", "2.849 tickets"),
    ]
    usable = CONTENT_W - gap * (len(segments) - 1)

    x = PAD
    for share, colour, label, label_ink, name, count in segments:
        width = usable * share / 100
        rect(slide, x, top, width, height, colour)
        text(
            slide,
            label,
            x,
            top + Inches(0.29),
            width,
            Inches(0.3),
            size=13,
            color=label_ink,
            bold=True,
            align=PP_ALIGN.CENTER,
        )
        # Legend directly beneath its own segment - no separate key to match up.
        # A segment narrower than its own label (human_only is 5,4 % of the width)
        # would push the text past the right margin, so the block keeps its full
        # width and slides left to stay inside the content area instead.
        block = max(width, Inches(1.62))
        block_x = min(x, PAD + CONTENT_W - block)
        rect(
            slide,
            block_x,
            top + height + Inches(0.22),
            Inches(0.34),
            Inches(0.07),
            colour,
        )
        text(
            slide,
            name,
            block_x,
            top + height + Inches(0.38),
            block,
            Inches(0.22),
            size=10,
            color=INK,
            bold=True,
        )
        text(
            slide,
            count,
            block_x,
            top + height + Inches(0.62),
            block,
            Inches(0.22),
            size=9.5,
            color=MUTED,
        )
        x += width + gap

    rect(slide, PAD, Inches(5.75), CONTENT_W, Emu(9525), LINE)
    text(
        slide,
        "“AI-addressable” combines the first two bars: AI produces the answer or "
        "carries out the action. Whether a human signs it off afterwards is a "
        "separate question — and for a third of all tickets, the answer is yes.",
        PAD,
        Inches(6.0),
        Inches(11.2),
        Inches(0.8),
        size=12.5,
        color=INK_2,
        line=1.4,
    )
    source(slide, "n = 52.694 classified tickets · shares of total volume")


def slide_modes(slide):
    eyebrow(slide, "How to read the four modes")
    heading(slide, "Capability is judged per ticket, not per category")

    modes = [
        (
            "fully_automatable",
            "18,8 %",
            R1,
            WHITE,
            (
                "AI resolves it end to end, with no human reading it first. Typically a "
                "question answerable from product documentation."
            ),
        ),
        (
            "ai_drafts_human_sends",
            "33,0 %",
            R2,
            WHITE,
            (
                "AI produces the complete answer or performs the action, but a human "
                "approves it — because money, insurer billing or patient data is involved."
            ),
        ),
        (
            "ai_assisted_triage",
            "42,8 %",
            R3,
            INK,
            (
                "AI cannot resolve it, but can summarise it, gather the missing facts and "
                "route it to the right team."
            ),
        ),
        (
            "human_only",
            "5,4 %",
            R4,
            INK,
            "Needs human judgement, a physical action, or a conversation.",
        ),
    ]
    top = Inches(1.95)
    row_h = Inches(1.26)
    for index, (name, share, colour, chip_ink, description) in enumerate(modes):
        y = top + index * row_h
        rect(slide, PAD, y, Inches(2.55), Inches(0.9), colour)
        text(
            slide,
            share,
            PAD,
            y + Inches(0.14),
            Inches(2.55),
            Inches(0.4),
            size=18,
            color=chip_ink,
            bold=True,
            align=PP_ALIGN.CENTER,
        )
        text(
            slide,
            name,
            PAD,
            y + Inches(0.58),
            Inches(2.55),
            Inches(0.24),
            size=9.5,
            color=chip_ink,
            align=PP_ALIGN.CENTER,
        )
        text(
            slide,
            description,
            PAD + Inches(2.9),
            y + Inches(0.12),
            Inches(8.5),
            Inches(0.9),
            size=13,
            color=INK_2,
            line=1.4,
        )

    source(slide, "Shares of 52.694 classified tickets")


def slide_buckets(slide):
    eyebrow(slide, "How tickets resolve")
    heading(
        slide,
        "Three ways a ticket ends — plus the ones we cannot read",
        "Every ticket was also classified by how it was actually resolved. This is the axis "
        "the business already recognises; the capability rating sits on top of it.",
    )

    rows = [
        (
            "support_todo",
            "A routine action a support rep performs in the system",
            16612,
            TODO,
            de(16612),
        ),
        (
            "question_answer",
            "Answerable from documentation; nothing changes in the system",
            12479,
            QA,
            de(12479),
        ),
        (
            "team_handoff",
            "Bug, missing feature, database fix, hardware or external vendor",
            12108,
            HANDOFF,
            de(12108),
        ),
        (
            "unclear",
            "The ticket text does not say what was wanted",
            11495,
            UNCLEAR,
            de(11495),
        ),
    ]
    row_h, gap, top = Inches(0.66), Inches(0.34), Inches(2.75)
    value_left = bar_rows(
        slide,
        rows,
        top,
        label_w=Inches(4.7),
        bar_w=Inches(4.9),
        max_value=16612,
        row_h=row_h,
        gap=gap,
    )

    for index, share in enumerate(["31,5 %", "23,7 %", "23,0 %", "21,8 %"]):
        y = top + index * (row_h + gap)
        text(
            slide,
            share,
            value_left + Inches(1.2),
            y + Inches(0.04),
            Inches(1.0),
            Inches(0.26),
            size=11,
            color=MUTED,
        )

    source(slide, "Bars show ticket counts; percentages are share of total volume")


def slide_validation(slide):
    eyebrow(slide, "Can the labels be trusted")
    heading(
        slide,
        "The tickets marked “another team” are the ones that really did escalate",
        "A model can label anything. The check uses two signals nobody produced for this "
        "analysis: whether a ticket was cloned into the development project AXT, and whether "
        "it ever sat in status “Frage an Fachabteilung”.",
    )

    rows = [
        ("team_handoff", de(12108) + " tickets", 2.73, HANDOFF, "2,73 %"),
        ("question_answer", de(12479) + " tickets", 0.54, QA, "0,54 %"),
        ("support_todo", de(16612) + " tickets", 0.50, TODO, "0,50 %"),
        ("unclear", de(11495) + " tickets", 0.07, UNCLEAR, "0,07 %"),
    ]
    label_w, bar_w = Inches(2.9), Inches(5.2)
    row_h, gap, top = Inches(0.54), Inches(0.22), Inches(3.15)
    bar_rows(
        slide,
        rows,
        top,
        label_w=label_w,
        bar_w=bar_w,
        max_value=3.0,
        row_h=row_h,
        gap=gap,
    )

    # Base-rate reference line, so each bar is read against the population.
    x = PAD + label_w + Inches(0.2) + bar_w * (0.93 / 3.0)
    rect(
        slide,
        x,
        top - Inches(0.28),
        Emu(12700),
        Inches(3.0),
        RGBColor(0x9A, 0xA8, 0xA5),
    )
    text(
        slide,
        "base rate 0,93 %",
        x - Inches(0.8),
        top - Inches(0.55),
        Inches(1.6),
        Inches(0.24),
        size=9.5,
        color=MUTED,
        align=PP_ALIGN.CENTER,
    )

    panel_left = PAD + Inches(9.3)
    rect(slide, panel_left, top, Inches(2.35), Inches(2.4), SURFACE)
    text(
        slide,
        "2,9×",
        panel_left,
        top + Inches(0.3),
        Inches(2.35),
        Inches(0.7),
        size=40,
        color=HANDOFF,
        bold=True,
        align=PP_ALIGN.CENTER,
    )
    text(
        slide,
        "the base rate of structural escalation — while unclear sits at a "
        "fortieth of it.",
        panel_left + Inches(0.2),
        top + Inches(1.05),
        Inches(1.95),
        Inches(1.1),
        size=11.5,
        color=INK_2,
        align=PP_ALIGN.CENTER,
        line=1.35,
    )

    source(
        slide,
        "The same check on a 46-ticket pilot showed 0,0 % everywhere and proved "
        "nothing — it only carries weight at full scale.",
    )


def slide_matrix(slide):
    eyebrow(slide, "Two questions, not one")
    heading(
        slide,
        "How a ticket resolves and whether AI can do it are different things",
        "A routine action can sit well within AI’s reach and still need a human signature. "
        "Judging the two separately is what makes the 51,8 % defensible.",
    )

    cols = [
        "fully automatable",
        "AI drafts, human sends",
        "AI-assisted triage",
        "human only",
    ]
    rows = [
        ("question_answer", QA, [(9693, 3), (2446, 2), (266, 1), (74, 1)]),
        ("support_todo", TODO, [(162, 1), (14854, 4), (944, 1), (652, 1)]),
        ("team_handoff", HANDOFF, [(0, 0), (77, 1), (10523, 3), (1508, 2)]),
        ("unclear", UNCLEAR, [(37, 1), (1, 1), (10842, 3), (615, 1)]),
    ]

    label_w, cell_w, cell_h, gap = Inches(2.5), Inches(2.15), Inches(0.76), Inches(0.05)
    grid_left = PAD + label_w
    head_top = Inches(2.95)

    for index, name in enumerate(cols):
        text(
            slide,
            name,
            grid_left + index * (cell_w + gap),
            head_top,
            cell_w,
            Inches(0.44),
            size=10,
            color=MUTED,
            bold=True,
            align=PP_ALIGN.CENTER,
            line=1.2,
        )

    top = head_top + Inches(0.52)
    for r, (name, colour, cells) in enumerate(rows):
        y = top + r * (cell_h + gap)
        rect(slide, PAD, y, Inches(0.05), cell_h, colour)
        text(
            slide,
            name,
            PAD + Inches(0.2),
            y + Inches(0.26),
            label_w,
            Inches(0.3),
            size=12,
            color=INK,
        )
        for c, (value, level) in enumerate(cells):
            x = grid_left + c * (cell_w + gap)
            rect(slide, x, y, cell_w, cell_h, M[level])
            text(
                slide,
                de(value),
                x,
                y + Inches(0.22),
                cell_w,
                Inches(0.34),
                size=14,
                bold=True,
                align=PP_ALIGN.CENTER,
                color=WHITE if level == 4 else (MUTED if level == 0 else INK),
            )

    text(
        slide,
        "Not one ticket routed to another team was judged fully automatable. "
        "That zero is the classification declining to overclaim.",
        PAD,
        Inches(6.62),
        Inches(11.2),
        Inches(0.4),
        size=12.5,
        color=INK_2,
    )
    source(
        slide, "Ticket counts · rows sum to the resolution split on the previous slide"
    )


def slide_categories(slide):
    eyebrow(slide, "Where it concentrates")
    heading(
        slide,
        "Three categories hold half the opportunity",
        "Ranked by AI-addressable ticket volume rather than by share, so the list points at "
        "where the work actually is.",
    )

    cats = [
        ("Abrechnung Kostenträger", 9010, 5500, "61,0 %"),
        ("Kunde", 6671, 4575, "68,6 %"),
        ("Stammdaten", 6327, 4238, "67,0 %"),
        ("Schnittstellen", 3930, 1737, "44,2 %"),
        ("Sonstige Themen", 4070, 1340, "32,9 %"),
        ("Terminplaner", 1966, 1246, "63,4 %"),
        ("Filialverwaltung / Berechtigungen", 1631, 1163, "71,3 %"),
        ("Kasse", 1841, 1106, "60,1 %"),
        ("Buchhaltung", 1550, 1095, "70,6 %"),
    ]
    rows = [
        (name, f"{de(total)} tickets total", addressable, QA, de(addressable))
        for name, total, addressable, _ in cats
    ]
    row_h, gap, top = Inches(0.46), Inches(0.07), Inches(2.55)
    value_left = bar_rows(
        slide,
        rows,
        top,
        label_w=Inches(3.6),
        bar_w=Inches(5.1),
        max_value=5500,
        row_h=row_h,
        gap=gap,
    )

    text(
        slide,
        "of category",
        value_left + Inches(1.2),
        top - Inches(0.3),
        Inches(1.2),
        Inches(0.22),
        size=9,
        color=MUTED,
        bold=True,
        spacing=1.0,
        caps=True,
    )
    for index, (_, _, _, share) in enumerate(cats):
        y = top + index * (row_h + gap)
        text(
            slide,
            share,
            value_left + Inches(1.2),
            y + Inches(0.02),
            Inches(1.0),
            Inches(0.24),
            size=10.5,
            color=MUTED,
        )

    text(
        slide,
        "Abrechnung Kostenträger, Kunde and Stammdaten together carry 14.313 "
        "addressable tickets — 52 % of the total opportunity.",
        PAD,
        Inches(7.0),
        Inches(11.2),
        Inches(0.4),
        size=12.5,
        color=INK_2,
    )
    source(
        slide,
        "Bar length is addressable ticket count; the percentage is that category’s "
        "addressable share",
    )


def slide_intents(slide):
    eyebrow(slide, "Discovered, not assumed")
    heading(
        slide,
        "The requests that keep coming back",
        "The classification named each ticket’s intent in its own words rather than picking "
        "from a list, so these groupings were found in the data. 38 intents recur 60 times or more.",
    )

    intents = [
        ("cloud tse aktivieren", 355, TODO, "94 %"),
        ("ax startet nicht", 323, HANDOFF, "3 %"),
        ("rechnung stornieren", 286, TODO, "96 %"),
        ("dokumentvorlage anpassen", 284, TODO, "81 %"),
        ("cloud tse einrichten", 203, TODO, "76 %"),
        ("drucker druckt nicht", 185, HANDOFF, "1 %"),
        ("noah startet nicht", 174, HANDOFF, "6 %"),
        ("amparex startet nicht", 160, HANDOFF, "6 %"),
        ("rueckruf anfordern", 158, UNCLEAR, "0 %"),
        ("kostentraegerabrechnung korrigieren", 126, TODO, "96 %"),
    ]
    rows = [(name, "", count, colour, str(count)) for name, count, colour, _ in intents]
    row_h, gap, top = Inches(0.4), Inches(0.07), Inches(2.6)
    value_left = bar_rows(
        slide,
        rows,
        top,
        label_w=Inches(3.5),
        bar_w=Inches(3.9),
        max_value=355,
        row_h=row_h,
        gap=gap,
    )

    share_left = value_left + Inches(0.85)
    text(
        slide,
        "AI-addressable",
        share_left,
        top - Inches(0.32),
        Inches(1.4),
        Inches(0.22),
        size=9,
        color=MUTED,
        bold=True,
        spacing=1.0,
        caps=True,
    )
    for index, (_, _, _, share) in enumerate(intents):
        y = top + index * (row_h + gap)
        text(
            slide,
            share,
            share_left,
            y + Inches(0.02),
            Inches(1.0),
            Inches(0.24),
            size=11,
            color=INK,
            bold=True,
        )

    panel_left = PAD + Inches(9.9)
    text(
        slide,
        "Dominant resolution",
        panel_left,
        top - Inches(0.32),
        Inches(2.4),
        Inches(0.22),
        size=9,
        color=MUTED,
        bold=True,
        spacing=1.0,
        caps=True,
    )
    for index, (name, colour) in enumerate(
        [("support_todo", TODO), ("team_handoff", HANDOFF), ("unclear", UNCLEAR)]
    ):
        y = top + index * Inches(0.32)
        rect(slide, panel_left, y + Inches(0.07), Inches(0.22), Inches(0.1), colour)
        text(
            slide,
            name,
            panel_left + Inches(0.32),
            y,
            Inches(2.1),
            Inches(0.24),
            size=10.5,
            color=INK_2,
        )

    text(
        slide,
        "The two callback intents — rueckruf anfordern and rueckruf erbeten, 284 "
        "tickets between them — carry no request in their text at all. That is an "
        "intake problem, not a modelling one.",
        panel_left,
        top + Inches(1.35),
        PAD + CONTENT_W - panel_left,
        Inches(1.8),
        size=11,
        color=INK_2,
        line=1.4,
    )

    source(
        slide,
        "Bar length is ticket count · colour is the intent’s dominant resolution type",
    )


def slide_caveats(slide):
    eyebrow(slide, "What this does not tell you")
    heading(slide, "Read these before acting on the number")

    notes = [
        (
            "A fifth of tickets cannot be judged",
            (
                "11.495 tickets (21,8 %) came back unclear — the text does not say what was "
                "wanted. That caps what any analysis of this data can claim, including this one."
            ),
        ),
        (
            "51,8 % is potential, not a plan",
            (
                "It says AI could produce the answer or carry out the action. It does not account "
                "for integration work, approval workflows, or the accuracy bar each category must "
                "clear before anything goes live."
            ),
        ),
        (
            "Callbacks hide the actual question",
            (
                "284 tickets are callback requests whose real content exists only in a voicemail. "
                "Capturing the request as text at intake would bring them into reach."
            ),
        ),
        (
            "Category names were reconstructed",
            (
                "The Jira Assets API returned 403 during the refresh, so category labels were "
                "restored from the local id–name map. 1.681 tickets (3,2 %) carry no category "
                "because none was ever set in Jira."
            ),
        ),
    ]
    col_w = (CONTENT_W - Inches(0.3)) / 2
    for index, (title, body) in enumerate(notes):
        x = PAD + (index % 2) * (col_w + Inches(0.3))
        y = Inches(2.15) + (index // 2) * Inches(2.25)
        rect(slide, x, y, col_w, Inches(1.95), SURFACE)
        rect(slide, x, y, Inches(0.05), Inches(1.95), R2)
        text(
            slide,
            title,
            x + Inches(0.34),
            y + Inches(0.28),
            col_w - Inches(0.68),
            Inches(0.34),
            size=14,
            color=INK,
            bold=True,
        )
        text(
            slide,
            body,
            x + Inches(0.34),
            y + Inches(0.74),
            col_w - Inches(0.68),
            Inches(1.05),
            size=11.5,
            color=INK_2,
            line=1.38,
        )


def slide_method(slide):
    eyebrow(slide, "Method")
    heading(slide, "How the classification was produced")

    steps = [
        (
            "Data",
            (
                "52.695 SDAX tickets pulled from Jira covering 01.01.2025 – 16.09.2026. "
                "21 months, no missing months, every ticket key unique."
            ),
        ),
        (
            "Input per ticket",
            (
                "Summary, description and all comments — the same text a support rep reads. "
                "Summary is present on every ticket, comments on 54 %."
            ),
        ),
        (
            "Classification",
            (
                "Each ticket labelled by an LLM with a resolution bucket, a sub-bucket, a "
                "free-text intent, an AI-capability mode, a confidence and one sentence of "
                "reasoning kept for spot-checking. 52.694 of 52.695 succeeded."
            ),
        ),
        (
            "Validation",
            (
                "Labels checked against structural signals already in the data — development "
                "clones and Fachabteilung status — which nobody created for this analysis."
            ),
        ),
        (
            "Cost",
            (
                "62,2M input and 10,9M output tokens over 3,03 hours. Results are cached per "
                "ticket, so re-running the analysis costs nothing."
            ),
        ),
    ]
    y = Inches(1.9)
    for label, body in steps:
        text(
            slide,
            label,
            PAD,
            y,
            Inches(2.0),
            Inches(0.3),
            size=12,
            color=INK,
            bold=True,
        )
        text(
            slide,
            body,
            PAD + Inches(2.2),
            y,
            Inches(9.2),
            Inches(0.72),
            size=12,
            color=INK_2,
            line=1.4,
        )
        y += Inches(0.96)

    source(
        slide,
        "Source notebook amparex_ai_assist_analysis.ipynb · per-ticket labels in "
        "sdax_ai_assist_classification.csv · median confidence 0,91–0,98 by bucket",
    )


SLIDES = (
    slide_title,
    slide_headline,
    slide_modes,
    slide_buckets,
    slide_validation,
    slide_matrix,
    slide_categories,
    slide_intents,
    slide_caveats,
    slide_method,
)


def build(output=OUTPUT):
    prs = Presentation()
    prs.slide_width, prs.slide_height = W, H
    for slide_fn in SLIDES:
        # Layout 6 is the blank layout: no inherited placeholders to fight.
        slide_fn(prs.slides.add_slide(prs.slide_layouts[6]))
    prs.save(output)
    return output


if __name__ == "__main__":
    print(f"wrote {build()} ({len(SLIDES)} slides)")
