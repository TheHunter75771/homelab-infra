// A minimal DOM for the tests: enough for the views, and an HTML parser behind innerHTML / outerHTML /
// insertAdjacentHTML so that a render that (wrongly) used them would really create elements and attributes
// from server strings. That is what makes the hostile test able to catch it (see mutation.test.mjs).

const HTML_NS = 'http://www.w3.org/1999/xhtml';
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr']);

class Node {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
  }
  get firstChild() {
    return this.childNodes[0] || null;
  }
  get children() {
    return this.childNodes.filter((n) => n.nodeType === 1);
  }
  appendChild(n) {
    if (n.nodeType === 11) {
      for (const c of [...n.childNodes]) this.appendChild(c);
      return n;
    }
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this;
    this.childNodes.push(n);
    return n;
  }
  append(...ns) {
    for (const n of ns) this.appendChild(typeof n === 'object' ? n : this.ownerDocument.createTextNode(String(n)));
  }
  insertBefore(n, ref) {
    if (!ref) return this.appendChild(n);
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = this;
    this.childNodes.splice(this.childNodes.indexOf(ref), 0, n);
    return n;
  }
  removeChild(n) {
    const i = this.childNodes.indexOf(n);
    if (i > -1) this.childNodes.splice(i, 1);
    n.parentNode = null;
    return n;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  replaceChildren(...ns) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    this.append(...ns);
  }
  contains(n) {
    for (let x = n; x; x = x.parentNode) if (x === this) return true;
    return false;
  }
  get textContent() {
    return this.childNodes.map((c) => c.textContent).join('');
  }
  set textContent(v) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    const s = String(v ?? '');
    if (s) this.appendChild(this.ownerDocument.createTextNode(s));
  }
}

class Text extends Node {
  constructor(doc, data) {
    super(doc);
    this.nodeType = 3;
    this.data = data;
  }
  get textContent() {
    return this.data;
  }
  set textContent(v) {
    this.data = String(v ?? '');
  }
  get nodeValue() {
    return this.data;
  }
}

class Fragment extends Node {
  constructor(doc) {
    super(doc);
    this.nodeType = 11;
  }
}

class ClassList {
  constructor(el) {
    this.el = el;
  }
  _get() {
    return (this.el.getAttribute('class') || '').split(/\s+/).filter(Boolean);
  }
  add(...c) {
    this.el.setAttribute('class', [...new Set([...this._get(), ...c])].join(' '));
  }
  remove(...c) {
    this.el.setAttribute('class', this._get().filter((x) => !c.includes(x)).join(' '));
  }
  contains(c) {
    return this._get().includes(c);
  }
  toggle(c, force) {
    const on = force ?? !this.contains(c);
    if (on) this.add(c);
    else this.remove(c);
    return on;
  }
}

class Element extends Node {
  constructor(doc, ns, name) {
    super(doc);
    this.nodeType = 1;
    this.namespaceURI = ns;
    this.localName = ns === HTML_NS ? name.toLowerCase() : name;
    this.tagName = ns === HTML_NS ? name.toUpperCase() : name;
    this.attributes = new Map();
    this.listeners = {};
    this.style = { setProperty(k, v) { this[k] = v; } };
    this.classList = new ClassList(this);
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.selected = false;
    this._value = '';
  }
  setAttribute(n, v) {
    this.attributes.set(this.namespaceURI === HTML_NS ? String(n).toLowerCase() : String(n), String(v));
  }
  getAttribute(n) {
    return this.attributes.has(n) ? this.attributes.get(n) : null;
  }
  hasAttribute(n) {
    return this.attributes.has(n);
  }
  removeAttribute(n) {
    this.attributes.delete(n);
  }
  get id() {
    return this.getAttribute('id') || '';
  }
  set id(v) {
    this.setAttribute('id', v);
  }
  get className() {
    return this.getAttribute('class') || '';
  }
  set className(v) {
    this.setAttribute('class', v);
  }
  get value() {
    if (this.localName === 'select') {
      const opts = this.descendants().filter((e) => e.localName === 'option');
      const sel = opts.find((o) => o.selected) || opts[0];
      return sel ? sel.getAttribute('value') ?? sel.textContent : '';
    }
    return this._value;
  }
  set value(v) {
    if (this.localName === 'select') {
      for (const o of this.descendants().filter((e) => e.localName === 'option')) o.selected = (o.getAttribute('value') ?? o.textContent) === String(v);
      return;
    }
    this._value = String(v);
  }
  descendants() {
    const out = [];
    const walk = (n) => {
      for (const c of n.childNodes) if (c.nodeType === 1) {
        out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  dispatchEvent(ev) {
    for (const fn of this.listeners[ev.type] || []) fn.call(this, ev);
    return true;
  }
  click() {
    this.dispatchEvent({ type: 'click', target: this, preventDefault() {} });
  }
  focus() {
    this.ownerDocument.activeElement = this;
  }
  // --- HTML parsing entry points: never used by the app; they exist so a regression is visible ---
  set innerHTML(html) {
    for (const c of [...this.childNodes]) this.removeChild(c);
    for (const n of parseHTML(this.ownerDocument, String(html))) this.appendChild(n);
  }
  get innerHTML() {
    return this.childNodes.map(serialize).join('');
  }
  set outerHTML(html) {
    const p = this.parentNode;
    if (!p) return;
    for (const n of parseHTML(this.ownerDocument, String(html))) p.insertBefore(n, this);
    p.removeChild(this);
  }
  insertAdjacentHTML(_pos, html) {
    for (const n of parseHTML(this.ownerDocument, String(html))) this.appendChild(n);
  }
}

function decode(s) {
  return s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}
function escape(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function serialize(n) {
  if (n.nodeType === 3) return escape(n.data);
  const attrs = [...n.attributes].map(([k, v]) => ` ${k}="${v.replace(/"/g, '&quot;')}"`).join('');
  return `<${n.localName}${attrs}>${n.childNodes.map(serialize).join('')}${VOID.has(n.localName) ? '' : `</${n.localName}>`}`;
}

/**
 * A small, forgiving HTML parser: tags, attributes (quoted, unquoted, bare), text, comments, void elements.
 * Its two patterns match in linear time on any input (dom-shim.test.mjs holds them to that):
 * - an attribute name holds no whitespace, quote, `<`, `>`, `/` or `=`, and an unquoted value no whitespace, quote,
 *   `<` or `>`. So each attribute reads only one way (the old pattern let `""` be a quoted or an unquoted value and
 *   backtracked exponentially: CodeQL js/redos), and a quote always opens or closes a quoted value, so a tag attempt
 *   that starts inside one can't fall into step with the attributes after it;
 * - a `<` outside a quoted value ends a tag attempt, and an unclosed `<!--` runs to the end of the input, so a failed
 *   attempt never rescans the rest of the input from every later `<`.
 * A comment ends where the WHATWG tokenizer ends one: at `-->` or `--!>`, at once for `<!-->` and `<!--->`, or at
 * the end of the input. The rest is not linear: an end tag walks up the open-element stack, so many unmatched end
 * tags cost depth x count.
 *
 * It departs from a browser in the unsafe direction for the hostile tests: each of these is TEXT here, while a
 * browser builds the element with its onerror, so hostile fixtures must not rely on them:
 *   a quote inside a name or an unquoted value   <img x"y onerror=…>   <img src=x' onerror=…>
 *   a `<` inside a name or an unquoted value     <img src=x<1 onerror=…>   (`<` + a letter starts a new tag instead)
 *   no whitespace after a quoted value           <img src="x"onerror=…>
 *   `/` between attributes (the old shim too)    <img/src=x onerror=…>
 */
export function parseHTML(doc, html) {
  const root = new Fragment(doc);
  const stack = [root];
  const re = /<\/?([a-zA-Z][\w-]*)((?:\s+[^\s"'<>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'<>]+))?)*)\s*\/?>|<!--(?:-?>|[\s\S]*?(?:--!?>|$))/g;
  let last = 0;
  let m;
  while ((m = re.exec(html))) {
    if (m.index > last) stack[stack.length - 1].appendChild(doc.createTextNode(decode(html.slice(last, m.index))));
    last = re.lastIndex;
    if (m[0].startsWith('<!--')) continue;
    const name = m[1].toLowerCase();
    if (m[0].startsWith('</')) {
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].localName === name) {
        stack.length = i;
        break;
      }
      continue;
    }
    const el = doc.createElement(name);
    const attrRe = /([^\s"'<>/=]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'<>]+))?/g; // the same name and value as re
    let a;
    while ((a = attrRe.exec(m[2] || ''))) el.setAttribute(a[1], a[2] ? decode(a[2].replace(/^["']|["']$/g, '')) : '');
    stack[stack.length - 1].appendChild(el);
    if (!VOID.has(name) && !m[0].endsWith('/>')) stack.push(el);
  }
  if (last < html.length) stack[stack.length - 1].appendChild(doc.createTextNode(decode(html.slice(last))));
  return [...root.childNodes];
}

class Document {
  constructor() {
    this.activeElement = null;
    this.body = new Element(this, HTML_NS, 'body');
    this.visibilityState = 'visible';
  }
  createElement(name) {
    return new Element(this, HTML_NS, name);
  }
  createElementNS(ns, name) {
    return new Element(this, ns, name);
  }
  createTextNode(data) {
    return new Text(this, String(data));
  }
  createDocumentFragment() {
    return new Fragment(this);
  }
  getElementById(id) {
    return [this.body, ...this.body.descendants()].find((e) => e.id === id) || null;
  }
  write() {
    throw new Error('document.write used');
  }
}

/** Installs a fresh document on globalThis and returns it. */
export function installDom() {
  const doc = new Document();
  globalThis.document = doc;
  return doc;
}

/** Every element under (and including) root. */
export function allElements(root) {
  const out = root.nodeType === 1 ? [root] : [];
  const walk = (n) => {
    for (const c of n.childNodes) if (c.nodeType === 1) {
      out.push(c);
      walk(c);
    }
  };
  walk(root);
  return out;
}
