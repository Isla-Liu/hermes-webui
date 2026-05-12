# PR Notes: Opt-in Smooth Streaming Text Fade

## Summary

Adds an opt-in `Fade text effect` preference for live assistant responses. When enabled, newly revealed words fade in during streaming for a smoother ChatGPT/Codex-like feel while preserving the existing default streaming path when disabled.

## User-facing behavior

- New setting: `Settings -> Preferences -> Fade text effect`
- Default: off
- Runtime flag: `window._fadeTextEffect`
- Fade mode uses a playout buffer so fast backend chunks do not land as large paragraph pops.
- Visual reveal rate is intentionally capped for readability, especially with very fast models.
- Live cursor is hidden while fade mode is active.
- Reduced-motion users get non-animated text.

## Implementation notes

- Fade mode is locked per stream to avoid mid-stream preference toggle rewind/duplication.
- Uses Hermes' existing incremental `streaming-markdown` parser with a custom renderer instead of full markdown re-renders.
- Only newly appended words are wrapped and animated.
- Animated spans are replaced with plain text by a delegated `animationend` handler, avoiding long-lived wrapper buildup without per-word listeners.
- Reduced-motion preference is cached with a media-query listener instead of checked for every appended text node.
- Unsafe streamed `href`/`src` values are blocked in the fade renderer `set_attr` path.
- On `done`, fade mode drains buffered text, ends the parser to flush pending markdown, and waits for the final fade/stagger window before the final `renderMessages()` replacement.

## Performance/readability tuning

- Normal fade duration: `160ms`
- Word stagger: `12ms`
- Done drain wait cap: `220ms`
- Visual playback is capped at `100 wps`.
- Reveals at most `2 words/frame`, or `3 words/frame` only with very large backlog.
- Reveal steps pause briefly after sentence punctuation and paragraph breaks so very fast models feel less overwhelming.
- Target word counts reuse the already-visible word count plus backlog count, avoiding repeated full-response word scans on each tick.

## Verification

```bash
node --check static/messages.js static/panels.js static/boot.js static/i18n.js
git diff --check
/Users/agent/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_smooth_text_fade.py tests/test_1003_preferences_autosave.py tests/test_streaming_markdown.py -q
/Users/agent/.hermes/hermes-agent/venv/bin/python -m py_compile api/config.py
```

Latest focused result:

```text
68 passed
```

## Manual QA suggested

- Hard refresh after deployment.
- Enable `Settings -> Preferences -> Fade text effect`.
- Test short normal response.
- Test long markdown response with headings, lists, links, code blocks, and tables.
- Test very fast model output around 200-400 tok/s.
- Test tool-call-heavy response.
- Test OS/browser reduced-motion mode if available.
