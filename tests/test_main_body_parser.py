import unittest

import aio_scraper as a


def page(body, cards=''):
    return ('<div id="search"><div id="rso"><div id="eKIzJc">'
            '<div data-subtree="aimc"><div jsname="KFl8ub">' + body +
            '</div></div>' + cards + '</div></div></div>')


class MainBodyParserTests(unittest.TestCase):
    def test_wrapper_prose_lists_tables_and_tail_preserve_order(self):
        html = page('''<div>Opening fact.<h4>Details</h4><p>Second fact.</p>
            <ul><li>First item<ul><li>Nested item</li></ul></li></ul>
            <table><tr><th>Policy</th><th>Support</th></tr>
            <tr><td>A</td><td>65%</td></tr></table>Closing fact.</div>''')
        blocks = a.parse_search_html(html, 'q', 'en', 'us')['ai_overview']['text_blocks']
        self.assertEqual([b['type'] for b in blocks],
                         ['paragraph', 'heading', 'paragraph', 'list', 'table', 'paragraph'])
        self.assertEqual(blocks[0]['snippet'], 'Opening fact.')
        self.assertEqual(blocks[3]['list'], [{'snippet': 'First item', 'list': [{'snippet': 'Nested item'}]}])
        self.assertEqual(blocks[4]['table'], [['Policy', 'Support'], ['A', '65%']])
        self.assertEqual(blocks[5]['snippet'], 'Closing fact.')

    def test_wrapped_google_list_items_preserve_all_text_and_nested_order(self):
        html = page('''<p>Intro.</p><h3>Policy positions</h3><ul>
            <div><li>First item<ul><div><li>Nested item</li></div></ul></li></div>
            <div><li>Second item <a href="https://source.example/">evidence</a></li></div>
            <div hidden><li>Hidden item</li></div>
            </ul><p>Tail.</p>''',
            '<div data-src-id="1"><a href="https://source.example/" aria-label="Study"></a></div>')
        blocks = a.parse_search_html(html, 'q', 'en', 'us')['ai_overview']['text_blocks']
        self.assertEqual([b['type'] for b in blocks], ['paragraph', 'heading', 'list', 'paragraph'])
        self.assertEqual(blocks[2]['list'][0], {'snippet': 'First item', 'list': [{'snippet': 'Nested item'}]})
        self.assertEqual(blocks[2]['list'][1]['snippet'], 'Second item evidence')
        self.assertEqual(blocks[2]['list'][1]['reference_indexes'], [0])
        self.assertEqual(len(blocks[2]['list']), 2)

    def test_nested_citations_inline_links_and_reference_snippets(self):
        html = page('''<ul><li>Parent<ul><li>Child <a href="https://source.example/">evidence</a></li></ul></li></ul>
            <p>Additional <a href="https://other.example/">reading</a>.</p>
            <table><tr><td>Fact <a href="https://source.example/">evidence</a></td></tr></table>''',
            '''<div data-src-id="1"><a href="https://source.example/" aria-label="Study - Publisher"></a>
            <div class="VwiC3b">Reference description.</div></div>''')
        aio = a.parse_search_html(html, 'q', 'en', 'us')['ai_overview']
        parent = aio['text_blocks'][0]['list'][0]
        self.assertEqual(parent['snippet'], 'Parent')
        self.assertNotIn('reference_indexes', parent)
        self.assertEqual(parent['list'][0]['reference_indexes'], [0])
        self.assertEqual(aio['text_blocks'][1]['snippet_links'],
                         [{'text': 'reading', 'link': 'https://other.example/'}])
        self.assertEqual(aio['text_blocks'][2]['detailed'][0][0]['reference_indexes'], [0])
        self.assertEqual(aio['references'][0]['snippet'], 'Reference description.')

    def test_semantic_layout_retains_mixed_unmarked_prose(self):
        html = '''<div id="search"><div id="eKIzJc"><div data-subtree="aimfl,mfl">
            Lead text.<p>Paragraph.</p><div>Tail text.</div><table><tr><td>Cell</td></tr></table>
            </div></div></div>'''
        blocks = a.parse_search_html(html, 'q', 'en', 'us')['ai_overview']['text_blocks']
        self.assertEqual([b['type'] for b in blocks], ['paragraph', 'paragraph', 'paragraph', 'table'])
        self.assertEqual([b.get('snippet') for b in blocks[:3]], ['Lead text.', 'Paragraph.', 'Tail text.'])

    def test_hidden_and_followup_content_does_not_leak(self):
        html = page('''<div>Keep <span hidden>secret</span><button>source chip</button> text.</div>
            <div>If you'd like, I can share information about:<ul><li>Suggestion</li></ul></div>''')
        aio = a.parse_search_html(html, 'q', 'en', 'us')['ai_overview']
        self.assertEqual(aio['text_blocks'], [{'type': 'paragraph', 'snippet': 'Keep text.'}])

    def test_organic_supporting_fields_only_when_observed(self):
        html = '''<div id="search"><div id="rso"><div class="g">
          <a href="https://example.org/article"><span class="VuuXrf">Publisher</span>
          <cite>example.org › article</cite><h3>Article</h3></a>
          <div class="VwiC3b">Full <em>snippet</em>.</div><time datetime="2026-01-02">Jan 2, 2026</time>
          <a rel="author" href="https://example.org/author">Jane</a>
          <div data-sitelinks><a href="https://example.org/details">Details</a></div>
          <div data-rich-snippet>16 pages</div>
          </div></div></div>'''
        item = a.parse_search_html(html, 'q', 'en', 'us')['organic_results'][0]
        self.assertEqual(item['source'], 'Publisher')
        self.assertEqual(item['displayed_link'], 'example.org › article')
        self.assertEqual(item['date'], 'Jan 2, 2026')
        self.assertEqual(item['author'], 'Jane')
        self.assertEqual(item['snippet_highlighted_words'], ['snippet'])
        self.assertEqual(item['sitelinks']['inline'], [{'title': 'Details', 'link': 'https://example.org/details'}])
        self.assertEqual(item['rich_snippet']['top']['extensions'], ['16 pages'])

    def test_unresolved_shell_differs_from_absent_and_explicit_unavailability(self):
        for markup, status in [('', 'absent'), ('<div id="eKIzJc"></div>', 'unresolved'),
                               ('<div id="eKIzJc">An AI Overview is not available for this search</div>', 'unavailable')]:
            result = a.parse_search_html('<div id="search">' + markup + '</div>', 'q', 'en', 'us')
            self.assertEqual(result['extraction_metadata']['aio_status'], status)

    def test_hidden_list_items_cells_and_comments_are_excluded(self):
        html = page('''<!-- editor comment --><p>Visible</p>
            <ul><li hidden>Hidden<ul><li>Hidden child</li></ul></li><li>Shown<ul hidden><li>Hidden nested list</li></ul></li></ul>
            <table><tbody hidden><tr><td>Hidden body</td></tr></tbody><tr hidden><td>Hidden row</td></tr><tr><td>A</td><td hidden>Hidden cell</td></tr></table>''')
        aio = a.parse_search_html(html, 'q', 'en', 'us')['ai_overview']
        self.assertNotIn('Hidden', str(aio))
        self.assertNotIn('editor comment', str(aio))
        self.assertEqual(aio['text_blocks'][1]['list'], [{'snippet': 'Shown'}])
        self.assertEqual(aio['text_blocks'][2]['table'], [['A']])

    def test_source_card_title_is_not_an_unavailable_notice(self):
        html = '<div id="search"><div id="eKIzJc"><div data-src-id="1">Data not available</div></div></div>'
        result = a.parse_search_html(html, 'q', 'en', 'us')
        self.assertEqual(result['extraction_metadata']['aio_status'], 'unresolved')

    def test_hidden_notice_templates_are_not_a_google_refusal(self):
        for hidden in ('style="display:none"', 'style="DISPLAY: none !important"',
                       'style="visibility: hidden"', 'hidden', 'aria-hidden="true"'):
            with self.subTest(hidden=hidden):
                html = ('<div id="search"><div id="eKIzJc"><div ' + hidden + '>'
                        '<span jsname="lGsj1">An AI Overview is not available for this search</span>'
                        '<span jsname="L1sC6e">Can\'t generate an AI overview right now. Try again later.</span>'
                        '</div><div role="progressbar" aria-label="Generating"></div></div></div>')
                result = a.parse_search_html(html, 'q', 'en', 'us')
                self.assertEqual(result['extraction_metadata']['aio_status'], 'unresolved')
                self.assertNotIn('ai_overview', result)

    def test_transient_generation_failure_is_unresolved_not_confirmed_unavailable(self):
        html = ('<div id="search"><div id="eKIzJc"><span jsname="L1sC6e">'
                "Can't generate an AI overview right now. Try again later.</span></div></div>")
        result = a.parse_search_html(html, 'q', 'en', 'us')
        self.assertEqual(result['extraction_metadata']['aio_status'], 'unresolved')
        self.assertEqual(result['extraction_metadata']['aio_reason'], 'generation_failed')

    def test_partial_answer_with_active_generation_is_not_complete(self):
        html = page('<p>Opening only.</p>')
        html = html.replace('<div data-subtree="aimc">',
                            '<div role="progressbar" aria-label="Generating"></div><div data-subtree="aimc">')
        result = a.parse_search_html(html, 'q', 'en', 'us')
        self.assertEqual(result['extraction_metadata']['aio_status'], 'unresolved')
        self.assertNotIn('ai_overview', result)

    def test_hidden_answer_is_not_present_and_hidden_prose_is_not_extracted(self):
        html = page('<p>Shown.</p><p style="display:none">Hidden answer text.</p>')
        result = a.parse_search_html(html, 'q', 'en', 'us')
        self.assertEqual(result['ai_overview']['text_blocks'], [{'type': 'paragraph', 'snippet': 'Shown.'}])
        result = a.parse_search_html(html.replace('id="eKIzJc"', 'id="eKIzJc" style="display:none"'), 'q', 'en', 'us')
        self.assertEqual(result['extraction_metadata']['aio_status'], 'unresolved')

    def test_copy_excluded_source_popovers_do_not_become_answer_paragraphs(self):
        html = page('<p>Main answer.</p><div data-ignore-copy><p>Source-card title and date.</p></div>',
                    '<div data-src-id="1"><a href="https://source.example/" aria-label="Study"></a></div>')
        aio = a.parse_search_html(html, 'q', 'en', 'us')['ai_overview']
        self.assertEqual(aio['text_blocks'], [{'type': 'paragraph', 'snippet': 'Main answer.'}])
        self.assertEqual(len(aio['references']), 1)

    def test_prompt_only_fallback_is_not_an_answer(self):
        html = ('<div id="search"><div id="eKIzJc"><div data-subtree="aimc">'
                '<h3 data-ignore-copy>AI Mode reply for q</h3></div></div></div>')
        result = a.parse_search_html(html, 'q', 'en', 'us')
        self.assertEqual(result['extraction_metadata']['aio_status'], 'unresolved')

    def test_one_response_json_preserves_nested_lists_and_tables(self):
        from comparison_storage import normalize_native_snapshot
        result = a.parse_search_html(page('<ul><li>Parent<ul><li>Child</li></ul></li></ul>'
                                         '<table><tr><td>A</td><td>B</td></tr></table>'), 'q', 'en', 'us')
        data = normalize_native_snapshot({
            'run_id': '00000000-0000-0000-0000-000000000001', 'captured_at': '2026-01-01T00:00:00Z',
            'results': [{'query_id': 'P01', 'result': result}]})
        self.assertEqual(len(data['queries']), 1)
        blocks = data['queries'][0]['aio_response']['blocks']
        self.assertEqual(blocks[0]['list'][0]['list'][0]['snippet'], 'Child')
        self.assertEqual(blocks[1]['table'], [['A', 'B']])


if __name__ == '__main__':
    unittest.main()
