"""Document creation and reading: DOCX, PPTX, XLSX, PDF, Markdown, HTML, TXT.

All formats share one block model so the model can author content once:

  spec = {
    "title": "...", "subtitle": "...", "author": "...",
    "blocks": [
      {"type": "heading", "text": "...", "level": 1},
      {"type": "paragraph", "text": "..."},
      {"type": "bullets", "items": ["..."]},
      {"type": "numbered", "items": ["..."]},
      {"type": "table", "rows": [["h1","h2"], ["a","b"]], "header": true},
      {"type": "code", "text": "...", "language": "python"},
      {"type": "quote", "text": "..."},
      {"type": "image", "path": "...", "caption": "..."},
      {"type": "pagebreak"}
    ]
  }
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from ..core.security import wrap_untrusted
from ..core.types import ToolResult
from .base import ToolContext, ToolRegistry


# ---------------------------------------------------------------- normalization
def _spec(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = {"title": "Document", "blocks": [{"type": "paragraph", "text": raw}]}
    if not isinstance(raw, dict):
        raise ValueError("spec must be a JSON object")
    raw.setdefault("title", "Untitled")
    raw.setdefault("blocks", [])
    if isinstance(raw.get("blocks"), str):
        raw["blocks"] = [{"type": "paragraph", "text": raw["blocks"]}]
    return raw


def _norm_block(b: Any) -> dict[str, Any]:
    if isinstance(b, str):
        return {"type": "paragraph", "text": b}
    if not isinstance(b, dict):
        return {"type": "paragraph", "text": str(b)}
    t = (b.get("type") or b.get("kind") or "paragraph").lower()
    if t in ("h1", "h2", "h3", "heading1", "heading2", "heading3"):
        b["type"] = "heading"
        b["level"] = int(t[-1])
    elif t in ("bullet", "bullets", "list", "unordered_list"):
        b["type"] = "bullets"
        b["items"] = b.get("items") or b.get("bullets") or [b.get("text", "")]
    elif t in ("number", "numbered", "ordered_list"):
        b["type"] = "numbered"
        b["items"] = b.get("items") or [b.get("text", "")]
    elif t in ("text", "p"):
        b["type"] = "paragraph"
    elif t in ("table", "grid"):
        b["type"] = "table"
    else:
        b["type"] = t
    return b


# ---------------------------------------------------------------- markdown/html
def _table_to_md(rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    widths = max(len(r) for r in rows)
    norm = [[str(c) if c is not None else "" for c in r] + [""] * (widths - len(r)) for r in rows]
    out = ["| " + " | ".join(norm[0]) + " |", "| " + " | ".join(["---"] * widths) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in norm[1:]]
    return "\n".join(out)


def blocks_to_markdown(spec: dict[str, Any]) -> str:
    lines = []
    if spec.get("title"):
        lines.append(f"# {spec['title']}")
    if spec.get("subtitle"):
        lines.append(f"*{spec['subtitle']}*")
    if spec.get("author"):
        lines.append(f"By {spec['author']}")
    lines.append("")
    for raw in spec.get("blocks", []):
        b = _norm_block(raw)
        t = b.get("type")
        text = b.get("text") or b.get("content") or ""
        if t == "heading":
            lines.append(f"{'#' * (int(b.get('level', 1)) + 1)} {text}")
        elif t == "paragraph":
            lines.append(text)
        elif t == "bullets":
            lines += [f"- {i}" for i in b.get("items", [])]
        elif t == "numbered":
            lines += [f"{n}. {i}" for n, i in enumerate(b.get("items", []), 1)]
        elif t == "table":
            lines.append(_table_to_md(b.get("rows", [])))
        elif t == "code":
            lines.append(f"```{b.get('language', '')}\n{text}\n```")
        elif t == "quote":
            lines += ["> " + ln for ln in text.splitlines()]
        elif t == "image":
            lines.append(f"![{b.get('caption', '')}]({b.get('path', '')})")
        elif t == "pagebreak":
            lines.append("\n---\n")
        elif t in ("note", "callout"):
            lines.append(f"> **{b.get('title', 'Note')}:** {text}")
        elif text:
            lines.append(str(text))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def blocks_to_html(spec: dict[str, Any]) -> str:
    parts = [f"<h1>{html.escape(spec.get('title') or '')}</h1>"]
    if spec.get("subtitle"):
        parts.append(f"<p class='subtitle'>{html.escape(spec['subtitle'])}</p>")
    if spec.get("author"):
        parts.append(f"<p class='author'>{html.escape(spec['author'])}</p>")
    for raw in spec.get("blocks", []):
        b = _norm_block(raw)
        t = b.get("type")
        text = html.escape(str(b.get("text") or ""))
        if t == "heading":
            lvl = min(6, int(b.get("level", 1)) + 1)
            parts.append(f"<h{lvl}>{text}</h{lvl}>")
        elif t == "paragraph":
            parts.append(f"<p>{text}</p>")
        elif t == "bullets":
            items = "".join(f"<li>{html.escape(str(i))}</li>" for i in b.get("items", []))
            parts.append(f"<ul>{items}</ul>")
        elif t == "numbered":
            items = "".join(f"<li>{html.escape(str(i))}</li>" for i in b.get("items", []))
            parts.append(f"<ol>{items}</ol>")
        elif t == "table":
            rows = b.get("rows", [])
            if rows:
                thead = "".join(f"<th>{html.escape(str(c))}</th>" for c in rows[0])
                body = "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows[1:])
                parts.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>")
        elif t == "code":
            parts.append(f"<pre><code>{html.escape(str(b.get('text') or ''))}</code></pre>")
        elif t == "quote":
            parts.append(f"<blockquote>{text}</blockquote>")
        elif t == "image":
            parts.append(f"<figure><img src='{html.escape(str(b.get('path', '')))}'/><figcaption>{html.escape(str(b.get('caption', '')))}</figcaption></figure>")
        elif t == "pagebreak":
            parts.append("<hr class='pagebreak'/>")
    css = """body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:860px;margin:48px auto;padding:0 24px;line-height:1.6;color:#1a1a1a}
h1{font-size:2.2em;border-bottom:2px solid #e5e5e5;padding-bottom:.3em}h2{margin-top:1.6em}
.subtitle{color:#666;font-size:1.15em;margin-top:-.4em}.author{color:#888;font-size:.95em}
table{border-collapse:collapse;width:100%;margin:1em 0}th,td{border:1px solid #ddd;padding:8px 10px;text-align:left}
th{background:#f5f5f5}code{background:#f4f4f4;padding:2px 5px;border-radius:4px}
pre{background:#f6f8fa;padding:16px;border-radius:8px;overflow:auto}blockquote{border-left:4px solid #ddd;margin:1em 0;padding:.2em 1em;color:#555}
figure{margin:1.4em 0;text-align:center}img{max-width:100%}.pagebreak{page-break-after:always;border:none}"""
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(spec.get('title') or '')}</title><style>{css}</style></head><body>{''.join(parts)}</body></html>"


# ---------------------------------------------------------------- DOCX
def _write_docx(spec: dict[str, Any], path: Path) -> None:
    from docx import Document
    from docx.enum.section import WD_SECTION
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    sec = doc.sections[0]
    sec.top_margin, sec.bottom_margin = Inches(1), Inches(1)
    sec.left_margin, sec.right_margin = Inches(1.1), Inches(1.1)

    styles = doc.styles
    normal = styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.15
    for name, size, color in (("Title", 32, "1F2937"), ("Heading 1", 20, "111827"),
                              ("Heading 2", 15, "374151"), ("Heading 3", 12.5, "4B5563")):
        st = styles[name]
        st.font.name = "Calibri"
        st.font.size = Pt(size)
        st.font.color.rgb = RGBColor.from_string(color)
        st.font.bold = True

    # header/footer
    hp = sec.header.paragraphs[0]
    hp.text = spec.get("title", "")
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for run in hp.runs:
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor(0x9C, 0xA3, 0xAF)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = fp.add_run()
    for instr in ("PAGE",):
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), instr)
        run._r.addnext(fld)
    run.font.size = Pt(8.5)

    if spec.get("title"):
        tp = doc.add_paragraph(spec["title"], style="Title")
        if spec.get("subtitle"):
            sp = doc.add_paragraph()
            r = sp.add_run(spec["subtitle"])
            r.font.size = Pt(14)
            r.font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
            sp.paragraph_format.space_after = Pt(4)
        if spec.get("author"):
            ap = doc.add_paragraph()
            r = ap.add_run(spec.get("author", ""))
            r.font.size = Pt(10)
            r.font.color.rgb = RGBColor(0x9C, 0xA3, 0xAF)
        if spec.get("date"):
            doc.add_paragraph(spec["date"]).runs[0].font.size = Pt(9)

    def shade(cell, hex_color: str) -> None:
        tcPr = cell._tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:fill"), hex_color)
        tcPr.append(shd)

    for raw in spec.get("blocks", []):
        b = _norm_block(raw)
        t = b.get("type")
        text = str(b.get("text") or b.get("content") or "")
        if t == "heading":
            doc.add_heading(text, level=min(4, max(1, int(b.get("level", 1)))))
        elif t == "paragraph":
            p = doc.add_paragraph()
            _add_rich_text(p, text)
        elif t == "bullets":
            for item in b.get("items", []):
                doc.add_paragraph(str(item), style="List Bullet")
        elif t == "numbered":
            for item in b.get("items", []):
                doc.add_paragraph(str(item), style="List Number")
        elif t == "quote":
            p = doc.add_paragraph(style="Intense Quote")
            _add_rich_text(p, text)
        elif t == "code":
            p = doc.add_paragraph()
            run = p.add_run(text)
            run.font.name = "Consolas"
            run.font.size = Pt(9)
            p.paragraph_format.left_indent = Inches(0.25)
            shade_paragraph(p, "F3F4F6")
        elif t == "table":
            rows = b.get("rows", [])
            if rows and isinstance(rows[0], dict):
                rows = [list(rows[0].keys())] + [[r.get(k, "") for k in rows[0]] for r in rows]
            if rows:
                cols = max(len(r) for r in rows)
                table = doc.add_table(rows=len(rows), cols=cols)
                table.style = "Light Grid Accent 1" if (b.get("style", "grid") != "plain") else "Table Grid"
                for ri, row in enumerate(rows):
                    for ci in range(cols):
                        cell = table.cell(ri, ci)
                        cell.text = str(row[ci]) if ci < len(row) else ""
                        if ri == 0 and b.get("header", True):
                            for r in cell.paragraphs[0].runs:
                                r.font.bold = True
                            shade(cell, "E5E7EB")
        elif t == "image":
            img = _resolve_image(b.get("path"), spec, path)
            if img and Path(img).exists():
                try:
                    doc.add_picture(img, width=Inches(float(b.get("width_inches", 5.8))))
                    if b.get("caption"):
                        cp = doc.add_paragraph(b["caption"])
                        cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        cp.runs[0].font.size = Pt(9)
                        cp.runs[0].font.italic = True
                except Exception:
                    doc.add_paragraph(f"[image: {img}]")
        elif t == "pagebreak":
            doc.add_page_break()
        elif text:
            _add_rich_text(doc.add_paragraph(), text)
    doc.save(str(path))


def shade_paragraph(p, hex_color: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    pPr = p._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    pPr.append(shd)


_BOLD = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")


def _add_rich_text(p, text: str) -> None:
    pos = 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            p.add_run(text[pos:m.start()])
        token = m.group(0)
        if token.startswith("**"):
            r = p.add_run(token[2:-2]); r.bold = True
        elif token.startswith("`"):
            r = p.add_run(token[1:-1]); r.font.name = "Consolas"
        else:
            r = p.add_run(token[1:-1]); r.italic = True
        pos = m.end()
    if pos < len(text):
        p.add_run(text[pos:])


def _resolve_image(img: str | None, spec: dict[str, Any], out_path: Path) -> str | None:
    if not img:
        return None
    p = Path(img)
    if not p.is_absolute():
        for base in (out_path.parent, Path(spec.get("_workspace", "."))):
            if (base / p).exists():
                return str(base / p)
    return str(p)


# ---------------------------------------------------------------- PDF
def _write_pdf(spec: dict[str, Any], path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    styles = getSampleStyleSheet()
    body = ParagraphStyle("ArgoBody", parent=styles["BodyText"], fontName="Helvetica",
                          fontSize=10.5, leading=15.5, spaceAfter=8, textColor=colors.HexColor("#1f2937"))
    h1 = ParagraphStyle("ArgoH1", parent=body, fontSize=20, leading=25, spaceBefore=14, spaceAfter=8,
                        textColor=colors.HexColor("#111827"), fontName="Helvetica-Bold")
    h2 = ParagraphStyle("ArgoH2", parent=h1, fontSize=14.5, leading=19, spaceBefore=10)
    title = ParagraphStyle("ArgoTitle", parent=h1, fontSize=30, leading=36, alignment=TA_CENTER)
    sub = ParagraphStyle("ArgoSub", parent=body, fontSize=13, alignment=TA_CENTER, textColor=colors.HexColor("#6b7280"))
    code = ParagraphStyle("ArgoCode", parent=body, fontName="Courier", fontSize=8.5, leading=11,
                          backColor=colors.HexColor("#f3f4f6"), borderPadding=6, spaceBefore=4, spaceAfter=10)

    def esc(t: str) -> str:
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        return re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", t)

    story: list[Any] = []
    if spec.get("title"):
        story += [Spacer(1, 1.1 * inch), Paragraph(esc(spec["title"]), title)]
        if spec.get("subtitle"):
            story.append(Spacer(1, 0.15 * inch))
            story.append(Paragraph(esc(spec["subtitle"]), sub))
        if spec.get("author"):
            story += [Spacer(1, 0.1 * inch), Paragraph(esc(str(spec["author"])), sub)]
        story.append(Spacer(1, 0.4 * inch))
    for raw in spec.get("blocks", []):
        b = _norm_block(raw)
        t = b.get("type")
        text = str(b.get("text") or b.get("content") or "")
        if t == "heading":
            story.append(Paragraph(esc(text), h1 if int(b.get("level", 1)) == 1 else h2))
        elif t == "paragraph":
            story.append(Paragraph(esc(text), body))
        elif t in ("bullets", "numbered"):
            for i, item in enumerate(b.get("items", []), 1):
                prefix = "•" if t == "bullets" else f"{i}."
                story.append(Paragraph(f"{prefix} {esc(str(item))}", body))
            story.append(Spacer(1, 4))
        elif t == "quote":
            story.append(Paragraph(f"<i>{esc(text)}</i>", ParagraphStyle("q", parent=body, leftIndent=18,
                                                                        textColor=colors.HexColor("#4b5563"))))
        elif t == "code":
            story.append(Paragraph(esc(text).replace("\n", "<br/>"), code))
        elif t == "table":
            rows = b.get("rows", [])
            if rows:
                tbl = Table([[Paragraph(esc(str(c)), body) for c in r] for r in rows], repeatRows=1)
                tbl.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3f4f6")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]))
                story += [Spacer(1, 4), tbl, Spacer(1, 10)]
        elif t == "image":
            img = _resolve_image(b.get("path"), spec, path)
            if img and Path(img).exists():
                try:
                    from PIL import Image as PILImage
                    with PILImage.open(img) as im:
                        w, h = im.size
                    width = float(b.get("width_inches", 5.6)) * inch
                    story.append(Image(img, width=width, height=width * h / max(w, 1)))
                except Exception:
                    story.append(Paragraph(f"[image: {img}]", body))
        elif t == "pagebreak":
            story.append(PageBreak())
        elif text:
            story.append(Paragraph(esc(text), body))
    if not story:
        story = [Paragraph("(empty document)", body)]

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                            topMargin=0.8 * inch, bottomMargin=0.8 * inch,
                            title=str(spec.get("title") or ""), author=str(spec.get("author") or ""))

    def on_page(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#9ca3af"))
        canvas.drawCentredString(A4[0] / 2, 0.45 * inch, f"{doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)


# ---------------------------------------------------------------- PPTX
THEMES = {
    "slate": {"bg": "0F172A", "fg": "F8FAFC", "accent": "38BDF8", "muted": "94A3B8"},
    "light": {"bg": "FFFFFF", "fg": "111827", "accent": "2563EB", "muted": "6B7280"},
    "warm": {"bg": "FFFBEB", "fg": "451A03", "accent": "D97706", "muted": "92400E"},
    "forest": {"bg": "052E16", "fg": "F0FDF4", "accent": "4ADE80", "muted": "86EFAC"},
    "mono": {"bg": "FAFAFA", "fg": "18181B", "accent": "52525B", "muted": "A1A1AA"},
}


def _write_pptx(spec: dict[str, Any], path: Path) -> None:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.util import Emu, Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    W, H = prs.slide_width, prs.slide_height
    theme = THEMES.get(str(spec.get("theme", "slate")).lower(), THEMES["slate"])

    def rgb(hex_: str) -> RGBColor:
        return RGBColor.from_string(hex_.replace("#", "").upper())

    blank = prs.slide_layouts[6]

    def rect(slide, x, y, w, h, color, line=False):
        shape = slide.shapes.add_shape(1, Emu(int(x)), Emu(int(y)), Emu(int(w)), Emu(int(h)))
        shape.fill.solid(); shape.fill.fore_color.rgb = rgb(color)
        if not line:
            shape.line.fill.background()
        shape.shadow.inherit = False
        return shape

    def textbox(slide, x, y, w, h, text, size=24, color=None, bold=False, align=PP_ALIGN.LEFT,
                font="Calibri", anchor=MSO_ANCHOR.TOP):
        tb = slide.shapes.add_textbox(Emu(int(x)), Emu(int(y)), Emu(int(w)), Emu(int(h)))
        tf = tb.text_frame; tf.word_wrap = True; tf.vertical_anchor = anchor
        tf.margin_left = tf.margin_right = Inches(0.05)
        lines = str(text).split("\n")
        for i, line in enumerate(lines):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align
            run = p.add_run(); run.text = line
            run.font.size = Pt(size); run.font.name = font; run.font.bold = bold
            run.font.color.rgb = rgb(color or theme["fg"])
        return tb

    def new_slide(bg: str | None = None):
        s = prs.slides.add_slide(blank)
        rect(s, 0, 0, W, H, bg or theme["bg"])
        return s

    # title slide
    s = new_slide(theme["bg"])
    rect(s, 0, 0, Inches(0.22), H, theme["accent"])
    textbox(s, Inches(1.0), Inches(2.1), W - Inches(2.0), Inches(1.6), spec.get("title", "Untitled"),
            size=44, bold=True, color=theme["fg"])
    if spec.get("subtitle"):
        textbox(s, Inches(1.0), Inches(3.8), W - Inches(2.0), Inches(1.0), spec["subtitle"],
                size=21, color=theme["muted"])
    if spec.get("author"):
        textbox(s, Inches(1.0), H - Inches(1.0), W - Inches(2.0), Inches(0.5), spec["author"],
                size=13, color=theme["muted"])

    for raw in spec.get("slides", spec.get("blocks", [])):
        sl = _norm_block(raw) if not isinstance(raw, dict) else dict(raw)
        layout = (sl.get("layout") or "bullets").lower()
        title = sl.get("title", "")
        s = new_slide(theme.get("bg") if layout != "section" else theme["accent"])
        # slide frame
        if layout == "section":
            textbox(s, Inches(1.0), Inches(2.6), W - Inches(2.0), Inches(1.4), title, size=40, bold=True,
                    color=theme["bg"], align=PP_ALIGN.CENTER)
            if sl.get("subtitle"):
                textbox(s, Inches(1.0), Inches(3.9), W - Inches(2.0), Inches(0.8), sl["subtitle"],
                        size=18, color=theme["bg"], align=PP_ALIGN.CENTER)
            continue

        # header band
        textbox(s, Inches(0.85), Inches(0.5), W - Inches(1.7), Inches(0.9), title, size=30, bold=True,
                color=theme["fg"])
        rect(s, Inches(0.85), Inches(1.42), Inches(1.5), Inches(0.06), theme["accent"])
        body_top = Inches(1.85)
        body_h = H - body_top - Inches(0.7)

        def bullets(items, x, w, size=20):
            tb = s.shapes.add_textbox(Emu(int(x)), Emu(int(body_top)), Emu(int(w)), Emu(int(body_h)))
            tf = tb.text_frame; tf.word_wrap = True
            for i, item in enumerate(items):
                level = 0
                if isinstance(item, dict):
                    text, level = item.get("text", ""), int(item.get("level", 0))
                else:
                    text = str(item)
                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                p.space_after = Pt(10)
                p.level = level
                run = p.add_run(); run.text = ("• " if level == 0 else "– ") + text
                run.font.size = Pt(size - level * 2); run.font.name = "Calibri"
                run.font.color.rgb = rgb(theme["fg"] if level == 0 else theme["muted"])
            return tb

        if layout == "bullets":
            bullets(sl.get("bullets") or sl.get("items") or [], Inches(0.9), W - Inches(1.8))
        elif layout == "two_column":
            bullets(sl.get("left") or sl.get("bullets", [])[: max(1, len(sl.get("bullets", [])) // 2)],
                    Inches(0.9), W / 2 - Inches(1.1), size=18)
            bullets(sl.get("right") or sl.get("bullets", [])[max(1, len(sl.get("bullets", [])) // 2):],
                    W / 2 + Inches(0.2), W / 2 - Inches(1.1), size=18)
        elif layout == "quote":
            textbox(s, Inches(1.2), Inches(2.3), W - Inches(2.4), Inches(2.2),
                    '"' + str(sl.get("quote") or sl.get("text") or "") + '"', size=30, color=theme["accent"],
                    align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
            if sl.get("attribution"):
                textbox(s, Inches(1.2), Inches(4.7), W - Inches(2.4), Inches(0.6), "— " + str(sl["attribution"]),
                        size=16, color=theme["muted"], align=PP_ALIGN.CENTER)
        elif layout == "image":
            img = _resolve_image(sl.get("image") or sl.get("path"), spec, path)
            if img and Path(img).exists():
                pic = s.shapes.add_picture(img, Inches(0.9), body_top, width=W - Inches(1.8))
                if sl.get("caption"):
                    textbox(s, Inches(0.9), H - Inches(0.95), W - Inches(1.8), Inches(0.5),
                            sl["caption"], size=12, color=theme["muted"], align=PP_ALIGN.CENTER)
            else:
                textbox(s, Inches(0.9), body_top, W - Inches(1.8), Inches(1.0), "[missing image]", size=18,
                        color=theme["muted"])
        elif layout == "table":
            rows = sl.get("rows", [])
            if rows:
                cols = max(len(r) for r in rows)
                shape = s.shapes.add_table(len(rows), cols, Emu(int(Inches(0.9))), Emu(int(body_top)),
                                           Emu(int(W - Inches(1.8))), Emu(int(body_h * 0.8)))
                table = shape.table
                for ri, row in enumerate(rows):
                    for ci in range(cols):
                        cell = table.cell(ri, ci)
                        cell.text = str(row[ci]) if ci < len(row) else ""
                        for p in cell.text_frame.paragraphs:
                            for r in p.runs:
                                r.font.size = Pt(14)
                                r.font.color.rgb = rgb(theme["fg"])
                                r.font.bold = ri == 0
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = rgb(theme["accent"] if ri == 0 else theme["bg"])
        elif layout == "cards":
            cards = sl.get("cards") or []
            n = max(1, len(cards))
            cw = (W - Inches(1.8) - Inches(0.3) * (n - 1)) / n
            for i, c in enumerate(cards):
                x = Inches(0.9) + i * (cw + Inches(0.3))
                rect(s, x, body_top, cw, body_h * 0.75, theme["accent"])
                textbox(s, x + Inches(0.2), body_top + Inches(0.25), cw - Inches(0.4), body_h * 0.7,
                        (c.get("title", "") + "\n\n" + c.get("text", "")) if isinstance(c, dict) else str(c),
                        size=15, color=theme["bg"])
        elif layout in ("blank", "content"):
            pass
        else:
            bullets(sl.get("bullets") or sl.get("items") or [str(sl.get("text", ""))], Inches(0.9), W - Inches(1.8))
        if sl.get("notes"):
            s.notes_slide.notes_text_frame.text = str(sl["notes"])
    prs.save(str(path))


# ---------------------------------------------------------------- XLSX
def _write_xlsx(spec: dict[str, Any], path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    sheets = spec.get("sheets") or [{"name": "Sheet1", "rows": spec.get("rows", []), "header": True}]
    header_fill = PatternFill("solid", fgColor="1F2937")
    header_font = Font(bold=True, color="FFFFFF", size=11)
    thin = Side(style="thin", color="D1D5DB")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for sh in sheets:
        ws = wb.create_sheet(str(sh.get("name", "Sheet"))[:31])
        rows = sh.get("rows") or []
        header = sh.get("header", True)
        for ri, row in enumerate(rows, 1):
            if isinstance(row, dict):
                row = list(row.values())
            for ci, value in enumerate(row, 1):
                cell = ws.cell(row=ri, column=ci)
                cell.value = value
                cell.border = border
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                if ri == 1 and header:
                    cell.fill = header_fill; cell.font = header_font
        widths = sh.get("widths")
        if not widths:
            widths = []
            for ci in range(1, max((len(r) for r in rows if isinstance(r, (list, tuple))), default=1) + 1):
                longest = max((len(str(r[ci - 1])) for r in rows if isinstance(r, (list, tuple)) and len(r) >= ci), default=8)
                widths.append(min(max(10, longest + 2), 60))
        for ci, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(ci)].width = w
        if header and rows:
            ws.freeze_panes = "A2"
        if sh.get("autofilter", True) and rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(max(len(r) for r in rows if isinstance(r,(list,tuple))))}{len(rows)}"
    wb.save(str(path))


# ---------------------------------------------------------------- readers
def _read_document(ctx: ToolContext, path: str, max_chars: int = 30_000) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"Not found: {p}", is_error=True)
    suffix = p.suffix.lower()
    text = ""
    if suffix == ".docx":
        from docx import Document
        doc = Document(str(p))
        parts: list[str] = []
        for par in doc.paragraphs:
            body = par.text.strip()
            if not body:
                continue
            style = (par.style.name if par.style is not None else "").lower()
            if style == "title":
                parts.append(f"TITLE: {body}")
            elif style == "subtitle":
                parts.append(f"SUBTITLE: {body}")
            elif style.startswith("heading"):
                digits = "".join(ch for ch in style.split()[-1] if ch.isdigit())
                level = max(1, min(6, int(digits) if digits else 1))
                parts.append("#" * level + " " + body)
            elif "list bullet" in style:
                parts.append(f"- {body}")
            elif "list number" in style:
                parts.append(f"1. {body}")
            elif "quote" in style:
                parts.append(f"> {body}")
            else:
                parts.append(body)
        for i, table in enumerate(doc.tables, 1):
            parts.append(f"TABLE {i} ({len(table.rows)}x{len(table.columns)}):")
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        text = "\n".join(parts)
    elif suffix == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(p))
        chunks = []
        for i, slide in enumerate(prs.slides, 1):
            chunks.append(f"\n--- Slide {i} ---")
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    chunks.append(shape.text_frame.text)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        chunks.append(" | ".join(c.text for c in row.cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                chunks.append("NOTES: " + slide.notes_slide.notes_text_frame.text)
        text = "\n".join(chunks)
    elif suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(str(p), data_only=True)
        chunks = []
        for ws in wb.worksheets:
            chunks.append(f"\n--- Sheet: {ws.title} ({ws.max_row}x{ws.max_column}) ---")
            for row in ws.iter_rows(values_only=True):
                if any(v is not None for v in row):
                    chunks.append(" | ".join("" if v is None else str(v) for v in row))
        text = "\n".join(chunks)
    elif suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        text = "\n\n".join((pg.extract_text() or "") for pg in reader.pages[:100])
    elif suffix in (".md", ".txt", ".html", ".csv", ".json"):
        text = p.read_text(encoding="utf-8", errors="replace")
    elif suffix == ".csv":
        text = p.read_text(encoding="utf-8", errors="replace")
    else:
        return ToolResult(f"Unsupported document type {suffix}", is_error=True)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated, {len(text):,} chars total]"
    if getattr(getattr(ctx.cfg, "security", None), "wrap_untrusted", True):
        text = wrap_untrusted(text, str(p), "document")
    return ToolResult(f"# {p}\n{text}", metadata={"path": str(p), "trusted": False})


def _create_document(ctx: ToolContext, path: str, spec: Any, format: str | None = None) -> ToolResult:
    p = ctx.resolve(path, write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    fmt = (format or p.suffix.lstrip(".") or "docx").lower()
    data = _spec(spec)
    try:
        if fmt == "docx":
            _write_docx(data, p)
        elif fmt in ("pptx", "ppt"):
            _write_pptx(data, p)
        elif fmt in ("xlsx", "xls", "csv"):
            if fmt == "csv":
                import csv
                rows = (data.get("sheets") or [{"rows": data.get("rows", [])}])[0].get("rows", [])
                with p.open("w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerows(rows)
            else:
                _write_xlsx(data, p)
        elif fmt == "pdf":
            _write_pdf(data, p)
        elif fmt in ("md", "markdown"):
            p.write_text(blocks_to_markdown(data), encoding="utf-8")
        elif fmt in ("html", "htm"):
            p.write_text(blocks_to_html(data), encoding="utf-8")
        elif fmt in ("txt", "text"):
            p.write_text(re.sub(r"[#*`>]", "", blocks_to_markdown(data)), encoding="utf-8")
        else:
            return ToolResult(f"Unsupported format: {fmt}", is_error=True)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Failed to create {fmt}: {type(exc).__name__}: {exc}", is_error=True)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    ctx.emit("document_created", path=str(p), format=fmt, bytes=size)
    return ToolResult(f"Created {fmt.upper()} at {p} ({size:,} bytes).", metadata={"path": str(p), "format": fmt})


def register(reg: ToolRegistry) -> None:
    params = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Output path, extension determines format if 'format' omitted."},
            "spec": {"type": "object", "description": (
                "Document spec. Common: {title, subtitle, author, blocks:[...]}. "
                "For PPTX use {title, subtitle, theme, slides:[{layout, title, bullets/subtitle/cards/rows, notes}]} "
                "with layout in title|bullets|section|two_column|quote|image|table|cards|blank. "
                "For XLSX use {sheets:[{name, rows:[[...]], header:true, widths:[]}]}. "
                "Blocks: heading, paragraph, bullets, numbered, table, code, quote, image, pagebreak.")},
            "format": {"type": "string", "enum": ["docx", "pptx", "xlsx", "pdf", "md", "html", "txt", "csv"]},
        },
        "required": ["path", "spec"],
    }
    reg.add("create_document",
            "Create a professional DOCX, PPTX, XLSX, PDF, Markdown, HTML, TXT, or CSV file from a structured JSON spec. "
            "Use for reports, proposals, slide decks, spreadsheets, and PDFs.",
            params, _create_document, read_only=False, timeout=180, category="documents")
    reg.add("read_document", "Extract text from DOCX, PPTX, XLSX, PDF, or text files (read your own output to verify).",
            {"type": "object", "properties": {"path": {"type": "string"},
                                              "max_chars": {"type": "integer", "default": 30000}}, "required": ["path"]},
            _read_document, read_only=True, max_output=40_000, category="documents")
