from pathlib import Path

REPO = Path(__file__).parent.parent
UI_JS = (REPO / 'static' / 'ui.js').read_text(encoding='utf-8')


def test_ios_standalone_detection_helper_exists():
    assert 'function _isIosStandalonePwa()' in UI_JS
    assert "window.matchMedia('(display-mode: standalone)').matches" in UI_JS
    assert 'navigator.standalone===true' in UI_JS


def test_message_bottom_prefers_one_pixel_inset_on_ios_pwa():
    assert 'function _messagePanePreferredBottomScrollTop(el)' in UI_JS
    assert 'return _isIosStandalonePwa()?maxTop-1:maxTop;' in UI_JS
    assert 'el.scrollTop=_messagePanePreferredBottomScrollTop(el);' in UI_JS


def test_ios_pwa_bottom_edge_guard_installed_on_messages_pane():
    assert 'function _maybeInsetIosStandaloneBottomEdge(el, top)' in UI_JS
    assert 'if(preferredTop<=0||top<el.scrollHeight-el.clientHeight) return;' in UI_JS
    assert '_maybeInsetIosStandaloneBottomEdge(el, top);' in UI_JS
