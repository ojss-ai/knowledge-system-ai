# tools/kb-confluence-sync/tests/test_xhtml_to_md.py
from xhtml_to_md import convert_storage_to_md


def test_basic_paragraph() -> None:
    html = "<p>Hello <strong>world</strong></p>"
    md = convert_storage_to_md(html)
    assert "Hello" in md
    assert "**world**" in md


def test_heading_conversion() -> None:
    html = "<h1>Title</h1><h2>Sub</h2>"
    md = convert_storage_to_md(html)
    assert "# Title" in md
    assert "## Sub" in md


def test_code_block() -> None:
    html = (
        '<ac:structured-macro ac:name="code"><ac:plain-text-body>'
        '<![CDATA[print("hi")]]></ac:plain-text-body></ac:structured-macro>'
    )
    md = convert_storage_to_md(html)
    assert "```" in md
    assert 'print("hi")' in md


def test_unknown_macro_becomes_named_fence() -> None:
    html = (
        '<ac:structured-macro ac:name="jira">'
        '<ac:parameter ac:name="key">KB-1</ac:parameter></ac:structured-macro>'
    )
    md = convert_storage_to_md(html)
    assert "```confluence-macro-jira" in md


def test_link_conversion() -> None:
    html = '<a href="https://example.com">click</a>'
    md = convert_storage_to_md(html)
    assert "[click](https://example.com)" in md


def test_table_conversion() -> None:
    html = """
    <table><tbody>
      <tr><th>Name</th><th>Value</th></tr>
      <tr><td>A</td><td>1</td></tr>
    </tbody></table>
    """
    md = convert_storage_to_md(html)
    assert "| Name |" in md
    assert "| A |" in md


def test_unknown_macro_captured_in_meta() -> None:
    html = (
        '<ac:structured-macro ac:name="widget">'
        '<ac:parameter ac:name="url">https://x.com</ac:parameter></ac:structured-macro>'
    )
    md, meta = convert_storage_to_md(html, return_meta=True)
    assert "widget" in meta.get("raw_macros", [{}])[0].get("name", "")
