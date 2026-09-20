#!/usr/bin/env python3
"""Stdlib-only Markdown -> self-contained HTML converter for evalup reports.

`run`/`analyze` write reports as Markdown; this renders a matching `.html` a
non-technical reviewer can double-click and read (docs/workflow.md, "Who does
what": "QA and the arbiter never need Claude Code: datasets are YAML, reports
are HTML/Markdown."). This script is the piece that promise depended on and that
was previously missing (QA punch-list gap: "missing .md->.html converter").

Supports exactly the subset the reports actually use: headings (#..######),
paragraphs, ordered/unordered lists (flat, one level), GFM pipe tables,
fenced code blocks + inline code, links, bold/italic. No external
dependencies, no network fetch, no template engine: the whole document (CSS
inlined in a <style> block) is one self-contained file that opens correctly
with no server and no internet connection.

Anything outside that subset (nested lists, blockquotes, raw HTML
passthrough, footnotes, task-list checkboxes, setext headings) is rendered as
plain escaped text rather than silently dropped or guessed at — a converter
that guesses produces a report that *looks* right and says the wrong thing,
which is worse than one that visibly does less. Fenced code content is never
interpreted as Markdown or HTML; it is escaped verbatim.

Usage: md_to_html.py <input.md> [output.html]
       (omit output to print the rendered HTML document to stdout)
Error contract: on unreadable/malformed input print {"error": "..."} to
stdout and exit 2; otherwise exit 0.
"""
import argparse
import html
import re

from _common import add_version_flag, load_text, write_output

# --- inline formatting -------------------------------------------------

CODE_SPAN_RE = re.compile(r"`([^`]+)`")
LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)')
# CommonMark's flanking rules, clause (1). A `*` can open emphasis only as part
# of a LEFT-flanking delimiter run ("not followed by Unicode whitespace") and
# close it only as part of a RIGHT-flanking one ("not preceded by Unicode
# whitespace") — spec 0.31.2 §6.2, rules 1 and 3. Without the two lookarounds,
# any two asterisks on a line paired up, so ordinary report prose came out
# mangled and looking deliberate: "Total: 5 * 3 * 2" rendered as "5 <em> 3 </em>
# 2", and a sentence naming two globs ("*.json and *.md") swallowed the text
# between them. Reports here routinely contain both arithmetic and glob
# patterns. Clauses (2a)/(2b), the punctuation cases, are still not implemented
# — this converter renders a documented subset, and the docstring's rule is that
# what it does not handle must come out as literal text rather than as a guess.
#
# The delimiter itself is also excluded from the span content. CommonMark pairs
# each closer with the NEAREST preceding opener (its delimiter-stack algorithm);
# a lazy `(.+?)` instead pairs the FIRST opener with that closer, which is a
# different string whenever an unmatched delimiter sits between them. In
# "globs *.json and *.md, plus *real* emphasis" the leading `*` of `*.json` is a
# legal opener (clause 2b: preceded by whitespace, followed by punctuation), so
# the lazy form ran it all the way to the closer after "real" and emphasized the
# sentence — while cmark emphasizes only "real". Excluding the delimiter from
# the content makes the leftmost candidate fail and the engine advance, which
# reproduces nearest-opener pairing for the single-delimiter spans this subset
# renders. Verified against the commonmark reference implementation.
_OPEN, _CLOSE = r"(?!\s)", r"(?<!\s)"
BOLD_RE = re.compile(
    rf"\*\*{_OPEN}((?:[^*]|\*(?!\*))+?){_CLOSE}\*\*"
    rf"|(?<!\w)__{_OPEN}((?:[^_]|_(?!_))+?){_CLOSE}__(?!\w)")
ITALIC_RE = re.compile(
    rf"\*{_OPEN}([^*]+?){_CLOSE}\*|(?<!\w)_{_OPEN}([^_]+?){_CLOSE}_(?!\w)")
_DANGEROUS_SCHEME = re.compile(r"^\s*(javascript|data|vbscript):", re.IGNORECASE)
# A private-use codepoint, never present in real Markdown/HTML input, used to
# stash code-span HTML so later link/bold/italic substitutions cannot reach
# inside it (a `**` inside inline code must stay literal, not become <strong>).
_STASH = "{}"
_STASH_RE = re.compile("(\\d+)")
_STASH_CHARS = re.compile("[]")


def safe_href(url):
    """The URL has already been html.escape(quote=False)'d by render_inline, so
    &<> are entities but quotes are raw. A raw `"` would break out of the href
    attribute and inject a live one (a report renders untrusted model output),
    so neutralize the quote chars quote=False left through, and refuse script-y
    schemes. Returns None to signal 'not a safe link' (render it as text)."""
    if _DANGEROUS_SCHEME.match(url):
        return None
    return url.replace('"', "&quot;").replace("'", "&#39;")


def plain_text(md):
    """Strip inline Markdown markers to bare text — for the <title>, which
    convert() escapes exactly once. (Escaping render_inline's already-escaped
    output a second time shows `&amp;amp;` in the browser tab.)"""
    md = LINK_RE.sub(lambda m: m.group(1), md)
    return re.sub(r"[*_`]", "", md)


def render_inline(text):
    """Escape then apply inline spans, in an order where code spans are
    protected from every later substitution (the code-quoted characters are
    data, not markup, however much they look like markup)."""
    # "A codepoint never present in real input" is an assumption about input,
    # and input here is model output — so make it true at the boundary rather
    # than defending it downstream. A document carrying the sentinel itself
    # either indexed past the stash list (IndexError, converter down) or, worse
    # and silently, landed in range and substituted an unrelated code span's
    # HTML into the document. Dropping stray sentinels up front means every
    # placeholder the unstash below sees is one this function put there.
    text = _STASH_CHARS.sub("", text)
    text = html.escape(text, quote=False)
    stashed = []

    def stash_code(m):
        stashed.append(f"<code>{m.group(1)}</code>")
        return _STASH.format(len(stashed) - 1)

    def render_link(m):
        href = safe_href(m.group(2))
        if href is None:
            return m.group(0)  # leave the (already-escaped) [text](url) literal
        return f'<a href="{href}">{m.group(1)}</a>'

    text = CODE_SPAN_RE.sub(stash_code, text)
    text = LINK_RE.sub(render_link, text)
    text = BOLD_RE.sub(
        lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = ITALIC_RE.sub(
        lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    return _STASH_RE.sub(lambda m: stashed[int(m.group(1))], text)


# --- block-level parsing -------------------------------------------------

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
UL_RE = re.compile(r"^[-*+]\s+(.*)$")
OL_RE = re.compile(r"^\d+\.\s+(.*)$")
FENCE_RE = re.compile(r"^```\s*([\w+-]*)\s*$")


def is_table_separator(line):
    """A GFM delimiter row: cells of only '-' (+ optional leading/trailing
    ':' for alignment), joined by '|'. Requires an actual '|' so a bare
    `---` line (which this converter does not treat as a setext heading) is
    never mistaken for one."""
    if "|" not in line:
        return False
    cells = strip_row(line).split("|")
    return bool(cells) and all(
        re.fullmatch(r":?-+:?", c.strip()) for c in cells)


def table_starts_at(lines, i, n):
    """True when a table header/separator pair begins at lines[i] — the same
    lookahead the block dispatcher and the paragraph terminator both need to
    agree on where a table interrupts a paragraph."""
    return i + 1 < n and "|" in lines[i] and is_table_separator(lines[i + 1])


def strip_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return line


def split_cells(row):
    """Split a table row on pipes that are neither escaped (\\|) nor inside a
    `code span`. A naive str.split('|') mis-counts columns for a cell like
    `` `a|b` `` or `x\\|y`, silently shifting every column after it."""
    row = strip_row(row)
    cells, buf, i, in_code = [], [], 0, False
    while i < len(row):
        ch = row[i]
        if ch == "\\" and i + 1 < len(row) and row[i + 1] == "|":
            buf.append("|")            # a literal pipe, unescaped into the cell
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
            buf.append(ch)
            i += 1
            continue
        if ch == "|" and not in_code:
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf))
    return cells


def cell_align(sep_cell):
    c = sep_cell.strip()
    left, right = c.startswith(":"), c.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    if left:
        return "left"
    return None


def render_table(header, sep, rows):
    aligns = [cell_align(c) for c in strip_row(sep).split("|")]

    def style(i):
        a = aligns[i] if i < len(aligns) else None
        return f' style="text-align:{a}"' if a else ""

    head_cells = [c.strip() for c in split_cells(header)]
    out = ["<table>", "<thead><tr>"]
    for i, c in enumerate(head_cells):
        out.append(f"<th{style(i)}>{render_inline(c)}</th>")
    out.append("</tr></thead>")
    out.append("<tbody>")
    for row in rows:
        cells = [c.strip() for c in split_cells(row)]
        out.append("<tr>")
        for i, c in enumerate(cells):
            out.append(f"<td{style(i)}>{render_inline(c)}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "\n".join(out)


def consume_list(lines, i, n, item_re, tag):
    """Consume consecutive lines matching item_re (UL_RE or OL_RE) starting at
    i; return the rendered <ul>/<ol> block and the index just past it. The
    item-accumulation loop is identical for ordered and unordered lists —
    only the regex and the wrapping tag differ."""
    items = []
    while i < n and item_re.match(lines[i]):
        items.append(item_re.match(lines[i]).group(1))
        i += 1
    lis = "\n".join(f"<li>{render_inline(it)}</li>" for it in items)
    return f"<{tag}>\n{lis}\n</{tag}>", i


def markdown_to_html_body(text):
    """Convert the supported Markdown subset to an HTML fragment (no
    <html>/<head>/<body> wrapper — the caller supplies that)."""
    lines = text.splitlines()
    out = []
    i, n = 0, len(lines)
    title = None

    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        fence = FENCE_RE.match(line.strip())
        if fence:
            lang = fence.group(1)
            body_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                body_lines.append(lines[i])
                i += 1
            i += 1  # consume closing fence (or EOF if unterminated)
            code = html.escape("\n".join(body_lines), quote=False)
            cls = f' class="language-{lang}"' if lang else ""
            out.append(f"<pre><code{cls}>{code}</code></pre>")
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            text_ = heading.group(2)
            if title is None and level == 1:
                title = plain_text(text_)   # raw text; convert() escapes once
            out.append(f"<h{level}>{render_inline(text_)}</h{level}>")
            i += 1
            continue

        if table_starts_at(lines, i, n):
            header, sep = line, lines[i + 1]
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(lines[i])
                i += 1
            out.append(render_table(header, sep, rows))
            continue

        if UL_RE.match(line):
            block, i = consume_list(lines, i, n, UL_RE, "ul")
            out.append(block)
            continue

        if OL_RE.match(line):
            block, i = consume_list(lines, i, n, OL_RE, "ol")
            out.append(block)
            continue

        # Paragraph: consume consecutive plain lines up to the next blank
        # line or the start of a block construct recognized above.
        para_lines = []
        while (i < n and lines[i].strip()
               and not HEADING_RE.match(lines[i])
               and not UL_RE.match(lines[i])
               and not OL_RE.match(lines[i])
               and not FENCE_RE.match(lines[i].strip())
               and not table_starts_at(lines, i, n)):
            para_lines.append(lines[i])
            i += 1
        out.append(f"<p>{render_inline(' '.join(para_lines))}</p>")

    return "\n".join(out), title


PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica,
      Arial, sans-serif;
    line-height: 1.55;
    max-width: 860px;
    margin: 2rem auto;
    padding: 0 1.25rem 4rem;
    color: #1a1a1a;
    background: #fff;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6e6e6; background: #14161a; }}
    a {{ color: #7db8ff; }}
    code, pre {{ background: #1f232a; }}
    table th {{ background: #23272e; }}
    table, th, td {{ border-color: #3a3f47; }}
    blockquote {{ border-left-color: #444; }}
  }}
  h1, h2, h3, h4, h5, h6 {{ line-height: 1.25; margin-top: 1.8em; }}
  h1 {{ border-bottom: 2px solid currentColor; padding-bottom: .3em; }}
  h2 {{ border-bottom: 1px solid #8884; padding-bottom: .25em; }}
  code {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: #8882;
    padding: .1em .35em;
    border-radius: .3em;
    font-size: .92em;
  }}
  pre {{
    background: #8882;
    padding: .9em 1em;
    border-radius: .5em;
    overflow-x: auto;
  }}
  pre code {{ background: none; padding: 0; }}
  table {{
    border-collapse: collapse;
    width: 100%;
    margin: 1em 0;
    display: block;
    overflow-x: auto;
  }}
  th, td {{ border: 1px solid #8886; padding: .4em .7em; text-align: left; }}
  th {{ background: #8881; }}
  a {{ color: #0b63c5; }}
  ul, ol {{ padding-left: 1.6em; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def convert(markdown_text, fallback_title):
    body, title = markdown_to_html_body(markdown_text)
    return PAGE_TEMPLATE.format(title=html.escape(title or fallback_title),
                                body=body)


def main():
    ap = argparse.ArgumentParser(
        description="Convert a Markdown file (headings, paragraphs, lists, "
                    "tables, code, links, bold/italic) into one "
                    "self-contained HTML file with inlined CSS.")
    add_version_flag(ap)
    ap.add_argument("input", help="Markdown file to convert")
    ap.add_argument("output", nargs="?", default=None,
                    help="destination .html path (default: print the "
                         "rendered document to stdout)")
    a = ap.parse_args()

    text = load_text(a.input)

    fallback_title = re.sub(r"\.md$", "", a.input.rsplit("/", 1)[-1]) or "Report"
    page = convert(text, fallback_title)

    write_output(a.output, page)


if __name__ == "__main__":
    main()
