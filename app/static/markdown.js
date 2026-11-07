/* Shared, sanitized Markdown + KaTeX renderer. */
(function (global) {
  'use strict';

  const MAX_SOURCE = 2 * 1024 * 1024;
  let parser = null;

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function mathHtml(source, displayMode) {
    const katex = global.katex;
    if (!katex || typeof katex.renderToString !== 'function') {
      return `<code class="math-fallback">${escapeHtml(source)}</code>`;
    }
    try {
      return katex.renderToString(source, {
        displayMode: !!displayMode,
        throwOnError: false,
        trust: false,
        strict: 'ignore',
        output: 'htmlAndMathml',
        maxExpand: 1000,
        maxSize: 50,
      });
    } catch (error) {
      return `<code class="math-error" title="${escapeHtml(error?.message || '公式渲染失败')}">${escapeHtml(source)}</code>`;
    }
  }

  const blockMath = {
    name: 'blockMath',
    level: 'block',
    start(src) {
      const match = /(?:^|\n)[ \t]*(?:\$\$|\\\[)/.exec(src);
      return match ? Math.max(0, match.index) : undefined;
    },
    tokenizer(src) {
      let match = /^[ \t]*\$\$([\s\S]+?)\$\$[ \t]*(?:\n+|$)/.exec(src);
      if (match) {
        return { type: 'blockMath', raw: match[0], text: match[1].trim(), displayMode: true };
      }
      match = /^[ \t]*\\\[([\s\S]+?)\\\][ \t]*(?:\n+|$)/.exec(src);
      if (match) {
        return { type: 'blockMath', raw: match[0], text: match[1].trim(), displayMode: true };
      }
      return undefined;
    },
    renderer(token) {
      return mathHtml(token.text, true);
    },
  };

  const inlineMath = {
    name: 'inlineMath',
    level: 'inline',
    start(src) {
      const match = /(?:\$|\\\()/.exec(src);
      return match ? match.index : undefined;
    },
    tokenizer(src) {
      let match = /^\\\(([\s\S]+?)\\\)/.exec(src);
      if (match) {
        return { type: 'inlineMath', raw: match[0], text: match[1].trim(), displayMode: false };
      }
      match = /^\$(?![\s$])((?:\\.|[^\\$\n])+?)\$(?!\d)/.exec(src);
      if (!match || /\s$/.test(match[1])) return undefined;
      return { type: 'inlineMath', raw: match[0], text: match[1], displayMode: false };
    },
    renderer(token) {
      return mathHtml(token.text, false);
    },
  };

  function getParser() {
    if (parser) return parser;
    if (!global.marked || typeof global.marked.Marked !== 'function') return null;
    parser = new global.marked.Marked({ gfm: true, breaks: false });
    parser.use({ extensions: [blockMath, inlineMath] });
    return parser;
  }

  function sanitize(html) {
    const purify = global.DOMPurify;
    if (!purify || typeof purify.sanitize !== 'function') return null;
    return purify.sanitize(html, {
      USE_PROFILES: { html: true, mathMl: true },
      FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'form', 'button',
        'textarea', 'select', 'option', 'link', 'meta', 'base'],
      FORBID_ATTR: ['srcset', 'formaction', 'xlink:href'],
      ALLOW_DATA_ATTR: false,
    });
  }

  function cleanLinksAndControls(html) {
    if (typeof document === 'undefined') return html;
    const template = document.createElement('template');
    template.innerHTML = html;
    template.content.querySelectorAll('a[href]').forEach(link => {
      const href = link.getAttribute('href') || '';
      if (/^https?:/i.test(href)) {
        link.setAttribute('target', '_blank');
        link.setAttribute('rel', 'noopener noreferrer');
      }
    });
    template.content.querySelectorAll('input').forEach(input => {
      const type = (input.getAttribute('type') || '').toLowerCase();
      if (type !== 'checkbox') {
        input.remove();
        return;
      }
      input.setAttribute('disabled', '');
      input.removeAttribute('name');
      input.removeAttribute('value');
    });
    template.content.querySelectorAll('img[src]').forEach(image => {
      const src = image.getAttribute('src') || '';
      if (!/^(https?:|data:image\/)/i.test(src)) image.removeAttribute('src');
    });
    return template.innerHTML;
  }

  function failure(message, source) {
    return `<div class="markdown-error" role="alert">${escapeHtml(message)}` +
      `<pre>${escapeHtml(source)}</pre></div>`;
  }

  function render(source, options) {
    const text = String(source ?? '').replace(/\r\n?/g, '\n');
    if (!text.trim()) {
      const emptyText = options && options.emptyText ? String(options.emptyText) : '没有正文';
      return `<p class="muted">${escapeHtml(emptyText)}</p>`;
    }
    if (text.length > MAX_SOURCE) return failure('Markdown 内容过大，未渲染。', '');
    const marked = getParser();
    if (!marked) return failure('Markdown 渲染组件未加载。', text);
    let html;
    try {
      html = marked.parse(text);
    } catch (error) {
      return failure(`Markdown 解析失败：${error?.message || error}`, text);
    }
    const safe = sanitize(html);
    if (safe === null) return failure('Markdown 安全过滤器未加载。', text);
    return cleanLinksAndControls(safe);
  }

  function renderInline(source) {
    const text = String(source ?? '').replace(/\r\n?/g, '\n');
    const marked = getParser();
    if (!marked) return escapeHtml(text);
    let html;
    try {
      html = marked.parseInline(text);
    } catch (_) {
      return escapeHtml(text);
    }
    const safe = sanitize(html);
    return safe === null ? escapeHtml(text) : cleanLinksAndControls(safe);
  }

  global.PapertexMarkdown = Object.freeze({
    render,
    renderInline,
    escapeHtml,
    maxSourceLength: MAX_SOURCE,
  });
})(window);
