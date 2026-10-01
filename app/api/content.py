"""Reference content (explainers, metadata, changelog, validation) taken from
the web tool's static/index.html, so plugins show Jamie's current text
without copying it. index.html is read, never changed or served.

Each item is the inner HTML of one modal, minus its Close / Got it button,
with scripts, form controls and inline event handlers removed. The styles
are inline and use the page's CSS variables (--text-primary etc.); the
values come back in css_variables so a client can set them on the
container it renders into.
"""
import hashlib
import os
import re
from html.parser import HTMLParser

from .problems import ApiProblem

# id -> (modal overlay element id, how to extract)
ITEMS = {
    'metadata': ('metadataModal', 'modal'),
    'changelog': ('changelogModal', 'modal'),
    'validation': ('validationModal', 'modal'),
    'tarr-explainer': ('ringInfoModal', 'modal'),
    'travel-time-explainer': ('travelTimeInfoModal', 'modal'),
    'scope-note': ('splashModal', 'note'),   # the "Note:" paragraph under the mode choice
}
TITLES = {'scope-note': 'Note'}

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
DROP = {'script', 'style', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'select', 'textarea', 'link', 'meta'}
EVENT_ATTR = re.compile(r'''\s+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)''', re.I)
JS_HREF = re.compile(r'''(\s+(?:href|src)\s*=\s*)(["']?)\s*javascript:[^"'>]*\2''', re.I)
CSS_VAR = re.compile(r'var\(\s*(--[a-z0-9-]+)', re.I)
ROOT_VARS = re.compile(r':root\s*\{([^}]*)\}', re.I)


class _Node:
    __slots__ = ('tag', 'attrs', 'start', 'open_end', 'close_start', 'end', 'children', 'parent', 'text')

    def __init__(self, tag, attrs, start, open_end, parent):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.start, self.open_end = start, open_end
        self.close_start = self.end = open_end
        self.children, self.text = [], []

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    def find(self, pred):
        return next((n for n in self.walk() if pred(n)), None)

    def classes(self):
        return (self.attrs.get('class') or '').split()

    def plain_text(self):
        return ' '.join(' '.join(n.text) for n in self.walk()).split()


class _TreeBuilder(HTMLParser):
    """Element tree with source offsets, so content is cut from the original
    text rather than re-serialized (keeps SVG attribute case and entities)."""

    def __init__(self, src):
        super().__init__(convert_charrefs=True)
        self.src = src
        self._line_starts = [0] + [m.end() for m in re.finditer('\n', src)]
        self.root = _Node('#root', [], 0, 0, None)
        self._stack = [self.root]

    def _offset(self):
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        start = self._offset()
        node = _Node(tag, attrs, start, start + len(self.get_starttag_text()), self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        start = self._offset()
        node = _Node(tag, attrs, start, start + len(self.get_starttag_text()), self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        if not any(n.tag == tag for n in self._stack[1:]):
            return  # stray end tag
        start = self._offset()
        end = self.src.index('>', start) + 1
        while True:
            node = self._stack.pop()
            if node.tag == tag:
                node.close_start, node.end = start, end
                return
            node.close_start = node.end = start  # implicitly closed

    def handle_data(self, data):
        self._stack[-1].text.append(data)


def _section(root, overlay_id, how):
    overlay = root.find(lambda n: n.attrs.get('id') == overlay_id)
    if overlay is None:
        return None
    modal = overlay.find(lambda n: n is not overlay and 'modal' in n.classes())
    if modal is None:
        return None
    if how == 'modal':
        return modal, modal.open_end, modal.close_start
    note = next((c for c in reversed(modal.children) if c.tag == 'p'), None)
    return (note, note.start, note.end) if note else None


def _removals(container, lo, hi):
    """(start, end) source ranges to cut: dropped elements, and a wrapper left
    holding nothing but a dropped control (the Close / Got it row)."""
    cuts = []
    for n in container.walk():
        if n is container or not (lo <= n.start < hi) or n.tag not in DROP:
            continue
        target = n
        p = n.parent
        if p is not container and p.tag == 'div' and all(c.tag in DROP for c in p.children) \
                and not ''.join(p.text).strip():
            target = p
        cuts.append((target.start, target.end))
    cuts.sort()
    merged = []
    for s, e in cuts:
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    return merged


def _clean(html):
    html = EVENT_ATTR.sub('', html)
    html = JS_HREF.sub(lambda m: m.group(1) + '"#"', html)
    html = re.sub(r'<!--.*?-->', '', html, flags=re.S)
    if re.search(r'<(pre|textarea)\b', html, re.I):
        return html.strip()
    # The page mixes tabs and spaces; leading whitespace means nothing in HTML.
    return '\n'.join(ln.strip() for ln in html.splitlines() if ln.strip())


class ContentStore:
    def __init__(self, path):
        self.path = path
        self._cache = None  # (mtime, size, digest, items)

    def get(self, item_id):
        if item_id not in ITEMS:
            raise ApiProblem(404, 'Not found', f'No content named {item_id!r}. '
                             f'Available: {", ".join(ITEMS)}.')
        digest, items = self._load()
        item = items.get(item_id)
        if item is None:
            raise ApiProblem(503, 'Content unavailable',
                             f'The {item_id!r} section was not found in the web tool page; '
                             'it may have been renamed upstream.')
        return digest, item

    def _load(self):
        try:
            st = os.stat(self.path)
        except OSError:
            raise ApiProblem(503, 'Content unavailable', 'The web tool page (static/index.html) is missing.')
        if self._cache and self._cache[:2] == (st.st_mtime_ns, st.st_size):
            return self._cache[2], self._cache[3]
        with open(self.path, 'rb') as f:
            raw = f.read()
        digest = hashlib.sha256(raw).hexdigest()
        src = raw.decode('utf-8')
        builder = _TreeBuilder(src)
        builder.feed(src)
        builder.close()
        m = ROOT_VARS.search(src)
        root_vars = {}
        if m:
            for decl in m.group(1).split(';'):
                if ':' in decl:
                    k, v = decl.split(':', 1)
                    if k.strip().startswith('--'):
                        root_vars[k.strip()] = v.strip()
        items = {}
        for item_id, (overlay_id, how) in ITEMS.items():
            found = _section(builder.root, overlay_id, how)
            if not found:
                continue
            node, lo, hi = found
            parts, pos = [], lo
            for s, e in _removals(node, lo, hi):
                parts.append(src[pos:s])
                pos = e
            parts.append(src[pos:hi])
            html = _clean(''.join(parts))
            h2 = node.find(lambda n: n.tag in ('h1', 'h2'))
            title = TITLES.get(item_id) or (' '.join(h2.plain_text()) if h2 else item_id)
            used = sorted(set(CSS_VAR.findall(html)))
            items[item_id] = {
                'id': item_id,
                'title': title,
                'html': html,
                'css_variables': {k: root_vars[k] for k in used if k in root_vars},
                'source': {'file': 'static/index.html', 'element_id': overlay_id, 'sha256': digest},
            }
        self._cache = (st.st_mtime_ns, st.st_size, digest, items)
        return digest, items
