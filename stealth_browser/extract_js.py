"""In-page JS injected by the snapshot pipeline to collect extractable elements.

Returns a list of {tag, bbox, xpath, css, text, attrs} for every element
on the page that's visible, inside the viewport, and either has its own
direct text or is a media/interactive element (img, a, button, input).

Downstream UIs render these as hover-highlightable overlay boxes on top
of a screenshot of the page. Keeping the traversal entirely in the page
context (rather than doing it server-side on the serialized HTML) means
the bboxes line up pixel-perfect with the screenshot.
"""

COLLECT_ELEMENTS_JS = r"""
(() => {
    // Cap output so pages with 10k+ DOM nodes (Amazon, LinkedIn, etc.)
    // don't blow past CDP's serialization budget. 3000 is enough for the
    // picker to cover the primary content region + a healthy margin.
    const MAX_ELEMENTS = 3000;
    // Tags we always collect even if they have no direct text (media/interactive)
    const ALWAYS = new Set(["A", "IMG", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "VIDEO"]);

    // Tags we skip entirely (structural/invisible/noise)
    const SKIP = new Set([
        "SCRIPT", "STYLE", "META", "LINK", "HEAD", "HTML", "BODY",
        "NOSCRIPT", "TEMPLATE", "SVG", "PATH", "DEFS", "G", "USE",
    ]);

    function directText(el) {
        let t = "";
        for (const n of el.childNodes) {
            if (n.nodeType === Node.TEXT_NODE) t += n.nodeValue;
        }
        return t.trim();
    }

    function isVisible(el) {
        const s = window.getComputedStyle(el);
        if (s.visibility === "hidden" || s.display === "none" || parseFloat(s.opacity) === 0) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) return false;
        return true;
    }

    function cssEscape(s) {
        if (window.CSS && CSS.escape) return CSS.escape(s);
        return String(s).replace(/([^\w-])/g, "\\$1");
    }

    // Detect per-element random IDs (UUIDs, long hex, generated SPA
    // container IDs). Amazon puts a fresh UUID on every product wrapper,
    // which poisons sibling detection — two products with the same
    // structure get different selectors because each is rooted at its
    // own unique id. We skip these so the selector walks past them and
    // uses the shared class/tag path instead.
    const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    function isRandomId(id) {
        if (!id) return false;
        if (UUID_RE.test(id)) return true;
        // 16+ char strings of only hex / random-looking chars are usually
        // generated too. Keep semantic ids like "main-content" (no long
        // pure-hex run) usable.
        if (id.length >= 16 && /^[0-9a-f-]+$/i.test(id) && !/-[a-z]{3,}/i.test(id)) return true;
        return false;
    }

    function buildCssSelector(el) {
        // Prefer id if CSS-safe, unique, AND not a per-element random id.
        if (el.id && /^[a-zA-Z0-9][\w-]*$/.test(el.id) && !isRandomId(el.id) && document.querySelectorAll("#" + el.id).length === 1) {
            return "#" + el.id;
        }
        const parts = [];
        let cur = el;
        while (cur && cur.nodeType === Node.ELEMENT_NODE && cur.tagName !== "HTML") {
            let part = cur.tagName.toLowerCase();
            if (cur.id && /^[a-zA-Z0-9][\w-]*$/.test(cur.id) && !isRandomId(cur.id)) {
                parts.unshift("#" + cur.id);
                break;
            }
            // Single useful class to stabilize (skip utility/hash classes)
            const cls = Array.from(cur.classList || []).find(
                c => /^[a-zA-Z][\w-]{1,40}$/.test(c) && !/^(is-|has-|css-|tw-|sc-|_)/.test(c)
            );
            if (cls) part += "." + cssEscape(cls);
            // Disambiguate with nth-of-type
            const parent = cur.parentElement;
            if (parent) {
                const sameTag = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
                if (sameTag.length > 1) {
                    const idx = sameTag.indexOf(cur) + 1;
                    part += `:nth-of-type(${idx})`;
                }
            }
            parts.unshift(part);
            cur = cur.parentElement;
        }
        return parts.join(" > ");
    }

    function buildXPath(el) {
        if (el.id && /^[a-zA-Z0-9][\w-]*$/.test(el.id) && !isRandomId(el.id) && document.querySelectorAll("#" + el.id).length === 1) {
            return `//*[@id="${el.id}"]`;
        }
        const parts = [];
        let cur = el;
        while (cur && cur.nodeType === Node.ELEMENT_NODE && cur.tagName !== "HTML") {
            const tag = cur.tagName.toLowerCase();
            const parent = cur.parentElement;
            if (!parent) { parts.unshift(tag); break; }
            const sameTag = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
            const idx = sameTag.indexOf(cur) + 1;
            parts.unshift(sameTag.length > 1 ? `${tag}[${idx}]` : tag);
            cur = parent;
        }
        return "/" + parts.join("/");
    }

    const collected = [];
    const all = document.querySelectorAll("*");
    const scrollY = window.scrollY || 0;
    const scrollX = window.scrollX || 0;

    all.forEach((el, idx) => {
        const tag = el.tagName;
        if (SKIP.has(tag)) return;
        if (!isVisible(el)) return;

        const text = directText(el);
        const isMedia = ALWAYS.has(tag);

        // Keep if element has own text, is a media/interactive el, or is a leaf
        const hasChildren = el.children.length > 0;
        const isLeaf = !hasChildren && text.length > 0;
        if (!isMedia && !isLeaf && text.length === 0) return;

        // Drop giant container boxes (full-width rows with huge height that
        // would just get in the way of clicking finer elements inside)
        const rect = el.getBoundingClientRect();
        if (rect.width > window.innerWidth * 0.95 && rect.height > window.innerHeight * 0.6 && !isMedia) {
            return;
        }

        const attrs = {};
        if (el.getAttribute("href")) attrs.href = el.getAttribute("href");
        if (el.getAttribute("src")) attrs.src = el.getAttribute("src");
        if (el.getAttribute("alt")) attrs.alt = el.getAttribute("alt");
        if (el.getAttribute("title")) attrs.title = el.getAttribute("title");
        if (el.getAttribute("aria-label")) attrs.aria_label = el.getAttribute("aria-label");
        if (el.value !== undefined && el.value !== "") attrs.value = el.value;

        let preview = text;
        if (!preview && attrs.alt) preview = attrs.alt;
        if (!preview && attrs.aria_label) preview = attrs.aria_label;
        if (!preview && attrs.title) preview = attrs.title;
        if (!preview && tag === "IMG" && attrs.src) preview = "[image]";
        if (!preview && tag === "A" && attrs.href) preview = "[link]";

        collected.push({
            id: idx,
            tag: tag.toLowerCase(),
            bbox: {
                x: Math.round(rect.left + scrollX),
                y: Math.round(rect.top + scrollY),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
            },
            xpath: buildXPath(el),
            css: buildCssSelector(el),
            text: preview.slice(0, 200),
            attrs: attrs,
        });
        if (collected.length >= MAX_ELEMENTS) return;
    });

    return {
        elements: collected,
        viewport: {
            width: window.innerWidth,
            height: window.innerHeight,
        },
        page: {
            width: document.documentElement.scrollWidth,
            height: document.documentElement.scrollHeight,
        },
        title: document.title,
        url: window.location.href,
    };
})()
"""
