# tools/kb-confluence-sync/xhtml_to_md.py
from __future__ import annotations

import re
from typing import Any, Literal, overload

try:
    import html2text
    from bs4 import BeautifulSoup, Tag
except ImportError as e:
    raise ImportError(
        "Install beautifulsoup4 and html2text: pip install beautifulsoup4 html2text"
    ) from e


@overload
def convert_storage_to_md(xhtml: str, return_meta: Literal[False] = False) -> str: ...


@overload
def convert_storage_to_md(xhtml: str, return_meta: Literal[True]) -> tuple[str, dict[str, Any]]: ...


def convert_storage_to_md(
    xhtml: str,
    return_meta: bool = False,
) -> str | tuple[str, dict[str, Any]]:
    """
    Convert Confluence XHTML storage format to Markdown.

    Rules (from kb-ingestion-connectors):
    - Known code macro → fenced code block
    - Unknown macros → named fenced block ```confluence-macro-<name> + raw XML in meta.raw_macros
    - Tables → GFM pipe tables
    - ac:link → resolve to plain URL where possible
    """
    meta: dict[str, Any] = {"raw_macros": []}

    soup = BeautifulSoup(xhtml, "html.parser")

    # Process structured macros
    for macro in soup.find_all("ac:structured-macro"):
        name_attr = macro.get("ac:name")
        name = name_attr if isinstance(name_attr, str) else "unknown"

        if name == "code":
            # Extract language parameter
            lang_tag = macro.find("ac:parameter", {"ac:name": "language"})
            lang = lang_tag.get_text(strip=True) if lang_tag else ""
            body_tag = macro.find("ac:plain-text-body")
            code = body_tag.get_text() if body_tag else ""
            fence = f"\n```{lang}\n{code}\n```\n"
            macro.replace_with(BeautifulSoup(f"<pre>{fence}</pre>", "html.parser"))

        elif name in ("panel", "info", "warning", "note", "tip"):
            title_tag = macro.find("ac:parameter", {"ac:name": "title"})
            title = title_tag.get_text(strip=True) if title_tag else name.capitalize()
            body_tag = macro.find("ac:rich-text-body") or macro.find("ac:plain-text-body")
            body = body_tag.get_text(strip=True) if body_tag else ""
            macro.replace_with(
                BeautifulSoup(
                    f"<blockquote><strong>{title}:</strong> {body}</blockquote>",
                    "html.parser",
                )
            )

        else:
            # Unknown macro — named fence + capture raw XML
            raw_xml = str(macro)
            fence = f"```confluence-macro-{name}\n{raw_xml}\n```"
            meta["raw_macros"].append({"name": name, "raw": raw_xml})
            macro.replace_with(BeautifulSoup(f"<pre>{fence}</pre>", "html.parser"))

    # Process ac:link tags → plain href
    for link in soup.find_all("ac:link"):
        href_tag = link.find("ri:url")
        anchor = link.find("ac:plain-text-link-body") or link.find("ac:link-body")
        if isinstance(href_tag, Tag):
            url_attr = href_tag.get("ri:value")
            url = url_attr if isinstance(url_attr, str) else "#"
            text = anchor.get_text(strip=True) if anchor else url
            link.replace_with(BeautifulSoup(f'<a href="{url}">{text}</a>', "html.parser"))
        else:
            link.replace_with(anchor.get_text(strip=True) if anchor else "")

    # Tables → GFM pipe tables. html2text cannot produce them (compact mode drops
    # the leading/trailing pipes, pad_tables pads cell widths), so convert directly
    # and re-insert after html2text via placeholders.
    table_blocks: list[str] = []
    for idx, table in enumerate(soup.find_all("table")):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [cell.get_text(strip=True) for cell in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)
        if not rows:
            table.decompose()
            continue
        lines = ["| " + " | ".join(rows[0]) + " |"]
        lines.append("|" + "|".join(" --- " for _ in rows[0]) + "|")
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        table_blocks.append("\n".join(lines))
        table.replace_with(f"kbtableplaceholder{idx}")

    # Convert remaining tags with html2text
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.body_width = 0
    h.protect_links = False
    h.wrap_links = False
    h.mark_code = True

    md = h.handle(str(soup))

    for idx, block in enumerate(table_blocks):
        md = md.replace(f"kbtableplaceholder{idx}", f"\n{block}\n")

    # Clean up html2text pre-block artefacts
    md = re.sub(r"\n{3,}", "\n\n", md).strip()

    if not meta["raw_macros"]:
        del meta["raw_macros"]

    if return_meta:
        return md, meta
    return md
