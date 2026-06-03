"""Tests for normalize_pre_blocks — the fix for the Quip→Outline content-loss
bug where lines starting with '#' (and any other bare text) inside Quip
prettyprint <pre> blocks were dropped by Outline's HTML→Markdown importer.

Root cause: Outline's importer treats <pre> containing <code> children as a
GFM-style fenced code block and extracts ONLY the <code> contents, dropping
sibling bare text and <br/>-separated lines.

Test data is drawn from real Quip export fragments where possible.
"""
import re

from quip_to_outline.cli import find_quip_blob_refs, normalize_pre_blocks


def _pre_inner(html):
    """Extract the inner text of the first normalized <pre class='code-block'>."""
    m = re.search(r'<pre class="code-block">(.*?)</pre>', html, re.S)
    assert m, f'no normalized <pre> in {html!r}'
    return m.group(1)


# ----- the real bug ------------------------------------------------------


def test_real_bug_hash_comment_lines_survive_alongside_inner_code_tags():
    """The original reproduction: <pre> with <code> children AND bare-text
    lines starting with '#' between them. Outline used to drop the bare text."""
    src = (
        "<pre id='temp:C:OYa248f02d' class='prettyprint'>"
        "<code>Pimlico</code><br/>"
        "Hot wallet<br/>"
        "LockDrop<br/><br/><br/>"
        "# фильтры мелкого хлама<br/>"
        "# Удаляем тег<br/>"
        "# ( длина тега &gt; 15 символов)<br/>"
        "# *pool*, *bridge*<br/>"
        "<code>#эти удаляем тоже</code>"
        "</pre>"
    )
    out = normalize_pre_blocks(src)
    inner = _pre_inner(out)
    # Every original line is present
    assert inner.splitlines() == [
        'Pimlico',
        'Hot wallet',
        'LockDrop',
        '',
        '',
        '# фильтры мелкого хлама',
        '# Удаляем тег',
        '# ( длина тега &gt; 15 символов)',
        '# *pool*, *bridge*',
        '#эти удаляем тоже',
    ]


# ----- structural transformations ----------------------------------------


def test_pre_gets_code_block_class_and_strips_quip_id():
    src = "<pre id='temp:C:X' class='prettyprint'>foo</pre>"
    out = normalize_pre_blocks(src)
    assert '<pre class="code-block">' in out
    assert "id='temp" not in out
    assert 'prettyprint' not in out


def test_nested_code_and_span_collapse_to_text():
    src = (
        "<pre class='prettyprint'>"
        "<code><span style='color:#212529'>Pimlico</span></code><br/>"
        "Hot wallet<br/>"
        "<code>WalletSimple</code>"
        "</pre>"
    )
    assert _pre_inner(normalize_pre_blocks(src)) == "Pimlico\nHot wallet\nWalletSimple"


def test_br_variants_all_become_newlines():
    src = "<pre>a<br>b<br/>c<br />d</pre>"
    assert _pre_inner(normalize_pre_blocks(src)) == "a\nb\nc\nd"


def test_nbsp_becomes_regular_space_so_columns_align():
    """Quip uses &nbsp; (\\xa0) between table-like columns in <pre>."""
    src = (
        "<pre><code>0xAAA</code>\xa0\xa0\xa0\xa0<code>0</code>"
        "\xa0\xa0\xa0\xa0<code>2427</code></pre>"
    )
    inner = _pre_inner(normalize_pre_blocks(src))
    assert inner == "0xAAA    0    2427"
    assert '\xa0' not in inner


def test_html_entities_decode_then_safe_reencode():
    src = "<pre>a &amp; b &lt;tag&gt; c &quot;d&quot;</pre>"
    inner = _pre_inner(normalize_pre_blocks(src))
    # & < > re-escaped for HTML safety; quote left as-is (safe in body text)
    assert inner == 'a &amp; b &lt;tag&gt; c "d"'


def test_numeric_entities_decoded():
    src = "<pre>price &#36;5 alpha &#x3B1;</pre>"
    assert _pre_inner(normalize_pre_blocks(src)) == "price $5 alpha α"


def test_multiple_blocks_each_normalized_independently():
    src = "<pre>a<br/>b</pre>middle<pre>c<br/>d</pre>"
    out = normalize_pre_blocks(src)
    assert out == (
        '<pre class="code-block">a\nb</pre>'
        'middle'
        '<pre class="code-block">c\nd</pre>'
    )


def test_preserves_leading_whitespace_for_indented_code():
    src = "<pre>def foo():<br/>    return 42</pre>"
    assert _pre_inner(normalize_pre_blocks(src)) == "def foo():\n    return 42"


def test_empty_block_yields_empty_code_block():
    assert normalize_pre_blocks("<pre></pre>") == '<pre class="code-block"></pre>'


def test_no_pre_blocks_returns_input_unchanged():
    src = "<p>just a paragraph</p>"
    assert normalize_pre_blocks(src) == src


def test_is_idempotent():
    src = "<pre id='x' class='prettyprint'><code>a</code><br/>b</pre>"
    once = normalize_pre_blocks(src)
    assert normalize_pre_blocks(once) == once


# ----- real Quip export fragments ----------------------------------------


def test_quip_logrotate_config_with_hash_comment_line():
    """Real fragment from AbVAAA6LmUZ: bare-text <pre> with leading # comment.
    Note: this case did NOT trigger the Outline bug (no inner <code> sibs),
    but the normalizer should still produce a clean code block.
    """
    src = (
        "<pre>"
        "# btw это буквально файл /etc/logrotate.d/api.ethplorer.io<br/>"
        "/srv/www/*/logs/*.log {<br/>"
        "  rotate 10<br/>"
        "  daily<br/>"
        "}"
        "</pre>"
    )
    inner = _pre_inner(normalize_pre_blocks(src))
    assert inner.startswith("# btw это буквально файл /etc/logrotate.d/api.ethplorer.io\n")
    assert "  rotate 10" in inner
    assert "  daily" in inner


# ----- find_quip_blob_refs -----------------------------------------------


def test_blob_ref_matches_img_src():
    html = "<img src='/blob/XEAAAARsNLv/U9Py-M-yopnKWrK2JXoMxA' style='x'/>"
    assert find_quip_blob_refs(html) == [
        ('XEAAAARsNLv', 'U9Py-M-yopnKWrK2JXoMxA'),
    ]


def test_blob_ref_matches_double_or_single_quotes():
    html = (
        "<img src='/blob/AAA111/BBB222'/>"
        '<a href="/blob/CCC333/DDD444">x</a>'
    )
    assert find_quip_blob_refs(html) == [
        ('AAA111', 'BBB222'),
        ('CCC333', 'DDD444'),
    ]


def test_blob_ref_deduplicates_repeated_references():
    html = (
        "<img src='/blob/AAA/BBB'/>"
        "<a href='/blob/AAA/BBB'>same</a>"
    )
    assert find_quip_blob_refs(html) == [('AAA', 'BBB')]


def test_github_absolute_url_is_not_matched():
    """Regression: previously the regex matched github paths and tried to
    fetch them as Quip blobs, then replaced them with '#'."""
    html = (
        '<a href="https://github.com/amilabs/admin-scripts/blob/master/'
        'ethplorer/nginx/ds10/nginx.conf">link</a>'
    )
    assert find_quip_blob_refs(html) == []


def test_github_url_in_text_node_is_not_matched():
    """The same github URL pasted as text (not in an attribute)."""
    html = '<p>https://github.com/o/r/blob/main/src/x.py</p>'
    assert find_quip_blob_refs(html) == []


def test_blob_id_with_dot_extension_is_not_matched():
    """Quip blob IDs never contain '.'; github filenames do (.py, .conf)."""
    html = "<a href='https://github.com/o/r/blob/main/x.py'>x</a>"
    assert find_quip_blob_refs(html) == []


def test_blob_id_path_with_slash_is_not_matched():
    """Quip blob IDs never contain '/'; github paths do."""
    html = "<a href='https://github.com/o/r/blob/main/sub/x'>x</a>"
    assert find_quip_blob_refs(html) == []


def test_mixed_quip_and_github_returns_only_quip():
    html = (
        '<a href="https://github.com/o/r/blob/main/src/x.py">gh</a>'
        "<img src='/blob/XEAAAARsNLv/U9Py-M-yopnKWrK2JXoMxA'/>"
        '<a href="https://github.com/o/r/blob/dev/path/file.conf">gh2</a>'
    )
    assert find_quip_blob_refs(html) == [
        ('XEAAAARsNLv', 'U9Py-M-yopnKWrK2JXoMxA'),
    ]


def test_quip_bash_script_with_amp_entities():
    """Real fragment from AAfAAATx89c: shell script with `&gt;/dev/null`."""
    src = (
        "<pre>"
        "#!/bin/bash<br/><br/>"
        "date +&quot;%T&quot;<br/><br/>"
        "for run in {1..10000}<br/>"
        "do<br/>"
        "  openssl aes-256-ecb -d -in 2.txt -pass pass:qweqweqwe &gt;/dev/null<br/>"
        "done"
        "</pre>"
    )
    inner = _pre_inner(normalize_pre_blocks(src))
    assert "#!/bin/bash" in inner
    assert 'date +"%T"' in inner
    # `>` was &gt; in source; round-trips back to &gt; in output (HTML-safe)
    assert "&gt;/dev/null" in inner
