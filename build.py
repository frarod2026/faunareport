#!/usr/bin/env python3
"""
Fauna Report static site generator.

No dependencies: Python 3.9+ standard library only.

    python3 build.py            # full build (fetches the feeds)
    python3 build.py --offline  # use the feed cache, no network access
    python3 build.py --serve    # build + local server at http://localhost:8000

Layout:
    config.json        site settings and feed list
    content/posts/     your own articles in markdown
    content/pages/     standalone pages (about, etc.)
    static/            files copied as-is into dist/
    dist/              output (do not edit by hand)
"""

import argparse
import html
import json
import os
import re
import shutil
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
CONTENT = ROOT / "content"
STATIC = ROOT / "static"
CACHE = ROOT / ".cache" / "feeds.json"

USER_AGENT = ("Mozilla/5.0 (compatible; FaunaReportBot/1.0; "
              "+https://faunareport.org; feed aggregator)")
FEED_TIMEOUT = 12
OG_EXT = "png"  # set by build_og_image(); referenced by head()

# --------------------------------------------------------------------------
# languages (English lives at the root; Portuguese under /pt/)
# --------------------------------------------------------------------------

LANGUAGES = [
    {"code": "en", "htmllang": "en", "prefix": "", "label": "EN",
     "posts": "posts", "pages": "pages", "default": True},
    {"code": "pt", "htmllang": "pt-BR", "prefix": "/pt", "label": "PT",
     "posts": "pt/posts", "pages": "pt/pages"},
]

STRINGS = {
    "en": {
        "nav_home": "Home", "nav_topics": "Topics", "nav_radar": "Radar",
        "nav_search": "Search", "nav_about": "About",
        "tagline": "What's happening to the world's animals, right now.",
        "skip": "Skip to content",
        "latest": "Latest", "written_here": "Written here", "read_next": "Read next",
        "in_this_article": "In this article", "article": "Article", "page": "Page",
        "min_read": "%d min read", "see_full_radar": "See the full radar",
        "radar_home_note": "Published elsewhere. Links open on the original site.",
        "radar_title": "Radar",
        "radar_desc": "A selection of articles published on other sites, linking to the source.",
        "radar_page_note": ("A selection of what came out elsewhere. Title, a short excerpt "
                            "and a link to the source — the full text stays where it was "
                            "published. %d items, refreshed on every build."),
        "sources": "Sources: %s.",
        "browse": "Browse", "topics_title": "Topics",
        "topics_desc": "Browse Fauna Report by topic — conservation, ecology, taxonomy and more.",
        "articles_on_topic": "%d %s on this topic.",
        "article_one": "article", "article_many": "articles",
        "find": "Find", "search_title": "Search",
        "search_desc": "Search Fauna Report articles.",
        "search_placeholder": "Search articles\u2026",
        "search_type": "Type to search %d articles.",
        "search_unavailable": "Search is unavailable right now.",
        "result_one": "result", "result_many": "results",
        "share": "Share", "copy_link": "Copy link", "copied": "Copied!",
        "footer_note": ("The articles here are written for this site. The Radar links to "
                        "other people\u2019s work, hosted by them."),
        "e404_eyebrow": "Error 404", "e404_title": "This page does not exist",
        "e404_body": ("The address may have changed, or never existed. Start from the "
                      "<a href=\"%s/\">home page</a> or check the <a href=\"%s/radar/\">Radar</a>."),
        "not_found_title": "Page not found",
        "not_found_desc": "The page you asked for does not exist.",
    },
    "pt": {
        "nav_home": "Início", "nav_topics": "Temas", "nav_radar": "Radar",
        "nav_search": "Buscar", "nav_about": "Sobre",
        "tagline": "O que está acontecendo com os animais do mundo, agora.",
        "skip": "Pular para o conteúdo",
        "latest": "Mais recente", "written_here": "Escrito aqui", "read_next": "Leia a seguir",
        "in_this_article": "Neste artigo", "article": "Artigo", "page": "Página",
        "min_read": "%d min de leitura", "see_full_radar": "Ver o radar completo",
        "radar_home_note": "Publicado em outros sites. Os links abrem na fonte original.",
        "radar_title": "Radar",
        "radar_desc": "Uma seleção de artigos publicados em outros sites, com link para a fonte.",
        "radar_page_note": ("Uma seleção do que saiu em outros lugares. Título, um trecho curto "
                            "e um link para a fonte — o texto completo fica onde foi publicado. "
                            "%d itens, atualizados a cada publicação."),
        "sources": "Fontes: %s.",
        "browse": "Explorar", "topics_title": "Temas",
        "topics_desc": "Explore o Fauna Report por tema — conservação, ecologia, taxonomia e mais.",
        "articles_on_topic": "%d %s sobre este tema.",
        "article_one": "artigo", "article_many": "artigos",
        "find": "Buscar", "search_title": "Buscar",
        "search_desc": "Busque nos artigos do Fauna Report.",
        "search_placeholder": "Buscar artigos\u2026",
        "search_type": "Digite para buscar em %d artigos.",
        "search_unavailable": "A busca está indisponível no momento.",
        "result_one": "resultado", "result_many": "resultados",
        "share": "Compartilhar", "copy_link": "Copiar link", "copied": "Copiado!",
        "footer_note": ("Os artigos aqui são escritos para este site. O Radar traz links para "
                        "o trabalho de outros, hospedado por eles."),
        "e404_eyebrow": "Erro 404", "e404_title": "Esta página não existe",
        "e404_body": ("O endereço pode ter mudado, ou nunca existiu. Comece pela "
                      "<a href=\"%s/\">página inicial</a> ou veja o <a href=\"%s/radar/\">Radar</a>."),
        "not_found_title": "Página não encontrada",
        "not_found_desc": "A página que você pediu não existe.",
    },
}

# current language context, set by main() before each language pass
L = {"code": "en", "htmllang": "en", "prefix": "", "label": "EN",
     "strings": STRINGS["en"]}
PT_SLUGS = set()   # article slugs that have a Portuguese translation


def T(key, *args):
    s = L["strings"].get(key)
    if s is None:
        s = STRINGS["en"].get(key, key)
    return (s % args) if args else s


def urlp(path):
    """Prefix a site-relative path with the current language dir (/pt/...)."""
    if not L["prefix"]:
        return path
    if path == "/":
        return L["prefix"] + "/"
    return L["prefix"] + path


def outp(*parts):
    """Output path under the current language directory."""
    if L["prefix"]:
        return DIST.joinpath(L["prefix"].strip("/"), *parts)
    return DIST.joinpath(*parts)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def slugify(text):
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-") or "sem-titulo"


def esc(text):
    return html.escape(str(text or ""), quote=True)


def strip_tags(text):
    text = re.sub(r"<script.*?</script>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text, limit):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:!?-") + "\u2026"


def read_time(text):
    words = len(re.findall(r"\w+", text))
    return max(1, round(words / 200))


def parse_date(value):
    """Aceita ISO 8601 e RFC 822. Devolve datetime com fuso ou None."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


EN_MONTHS = ["January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]

PT_MONTHS = ["janeiro", "fevereiro", "março", "abril", "maio", "junho",
             "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def pt_date(dt):
    if not dt:
        return ""
    if L["code"] == "pt":
        return "%d de %s de %d" % (dt.day, PT_MONTHS[dt.month - 1], dt.year)
    return "%s %d, %d" % (EN_MONTHS[dt.month - 1], dt.day, dt.year)


# --------------------------------------------------------------------------
# markdown (subset sufficient for articles)
# --------------------------------------------------------------------------

def inline_md(text):
    text = esc(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def render_markdown(md):
    """Returns (html, heading_index)."""
    lines = md.replace("\r\n", "\n").split("\n")
    out, toc = [], []
    i = 0
    para, list_items, list_type = [], [], None

    def flush_para():
        if para:
            out.append("<p>%s</p>" % inline_md(" ".join(para).strip()))
            para.clear()

    def flush_list():
        nonlocal list_type
        if list_items:
            tag = "ol" if list_type == "ol" else "ul"
            body = "".join("<li>%s</li>" % inline_md(x) for x in list_items)
            out.append("<%s>%s</%s>" % (tag, body, tag))
            list_items.clear()
            list_type = None

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            flush_para(); flush_list()
            lang = line.strip()[3:].strip()
            block = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            cls = ' class="lang-%s"' % esc(lang) if lang else ""
            out.append("<pre><code%s>%s</code></pre>" % (cls, esc("\n".join(block))))
            i += 1
            continue

        if not line.strip():
            flush_para(); flush_list()
            i += 1
            continue

        if re.match(r"^-{3,}\s*$", line.strip()):
            flush_para(); flush_list()
            out.append("<hr>")
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para(); flush_list()
            level = len(m.group(1))
            raw = m.group(2).strip()
            hid = slugify(raw)
            if level == 2:
                toc.append((hid, raw))
            out.append("<h%d id=\"%s\">%s</h%d>" % (level, hid, inline_md(raw), level))
            i += 1
            continue

        if line.strip().startswith(">"):
            flush_para(); flush_list()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote><p>%s</p></blockquote>" % inline_md(" ".join(quote)))
            continue

        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            flush_para()
            if list_type == "ol":
                flush_list()
            list_type = "ul"
            list_items.append(m.group(1).strip())
            i += 1
            continue

        m = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if m:
            flush_para()
            if list_type == "ul":
                flush_list()
            list_type = "ol"
            list_items.append(m.group(1).strip())
            i += 1
            continue

        flush_list()
        para.append(line.strip())
        i += 1

    flush_para(); flush_list()
    return "\n".join(out), toc


def parse_front_matter(raw):
    """Reads the --- ... --- block at the top of the file."""
    meta = {}
    raw = raw.lstrip("\ufeff")
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            block = raw[3:end].strip()
            raw = raw[end + 4:].lstrip("\n")
            for line in block.split("\n"):
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]  # strip only matching surrounding quotes
                key = key.strip()
                if key in ("tags", "keywords"):
                    meta[key] = [t.strip() for t in value.split(",") if t.strip()]
                else:
                    meta[key] = value
    return meta, raw


def load_documents(folder):
    docs = []
    if not folder.exists():
        return docs
    for path in sorted(folder.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        body_html, toc = render_markdown(body)
        plain = strip_tags(body_html)
        docs.append({
            "slug": meta.get("slug") or slugify(path.stem),
            "title": meta.get("title", path.stem),
            "description": meta.get("description", truncate(plain, 155)),
            "date": parse_date(meta.get("date")),
            "updated": parse_date(meta.get("updated")) or parse_date(meta.get("date")),
            "tags": meta.get("tags", []),
            "author": meta.get("author", ""),
            "image": meta.get("image", ""),
            "image_alt": meta.get("image_alt", ""),
            "image_credit": meta.get("image_credit", ""),
            "html": body_html,
            "toc": toc,
            "plain": plain,
            "read_time": read_time(plain),
            "noindex": str(meta.get("noindex", "")).lower() in ("1", "true", "sim"),
        })
    docs.sort(key=lambda d: d["date"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return docs


# --------------------------------------------------------------------------
# external feeds
# --------------------------------------------------------------------------

def fetch_feed(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=FEED_TIMEOUT) as resp:
        return resp.read()


def parse_feed(data, source):
    """Supports RSS 2.0 and Atom. Returns a list of normalised items."""
    items = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        print("    ! invalid XML: %s" % exc)
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom",
          "dc": "http://purl.org/dc/elements/1.1/",
          "content": "http://purl.org/rss/1.0/modules/content/"}

    nodes = root.findall(".//item")
    is_atom = False
    if not nodes:
        nodes = root.findall(".//atom:entry", ns)
        is_atom = True

    for node in nodes:
        if is_atom:
            title = node.findtext("atom:title", "", ns)
            link = ""
            for ln in node.findall("atom:link", ns):
                rel = ln.get("rel", "alternate")
                if rel == "alternate" and ln.get("href"):
                    link = ln.get("href")
                    break
            if not link:
                ln = node.find("atom:link", ns)
                link = ln.get("href", "") if ln is not None else ""
            summary = node.findtext("atom:summary", "", ns) or node.findtext("atom:content", "", ns)
            date_raw = node.findtext("atom:updated", "", ns) or node.findtext("atom:published", "", ns)
        else:
            title = node.findtext("title", "")
            link = node.findtext("link", "")
            summary = node.findtext("description", "") or node.findtext("content:encoded", "", ns)
            date_raw = node.findtext("pubDate", "") or node.findtext("dc:date", "", ns)

        title = strip_tags(title)
        link = (link or "").strip()
        if not title or not link.startswith("http"):
            continue

        dt = parse_date(date_raw)
        items.append({
            "title": title,
            "link": link,
            "summary": strip_tags(summary),
            "date": dt.isoformat() if dt else "",
            "source": source["name"],
            "source_url": source.get("site", ""),
        })
    return items


def collect_radar(config, offline=False):
    """Fetches the feeds, with an on-disk cache as a safety net."""
    cached = []
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = []

    if offline:
        print("  offline mode: using %d cached items" % len(cached))
        items = cached
    else:
        items, failures = [], 0
        for source in config.get("feeds", []):
            print("  reading %s" % source["name"])
            try:
                items.extend(parse_feed(fetch_feed(source["url"]), source))
            except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
                print("    ! failed (%s)" % exc)
                failures += 1
        if not items and cached:
            print("  no feed responded; falling back to cache (%d items)" % len(cached))
            items = cached
        elif items:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        if failures:
            print("  %d feed(s) did not respond" % failures)

    seen, unique = set(), []
    for it in items:
        if it["link"] in seen:
            continue
        seen.add(it["link"])
        unique.append(it)

    unique.sort(key=lambda x: x.get("date") or "", reverse=True)
    limit = config.get("radar_max_items", 24)
    chars = config.get("radar_excerpt_chars", 180)
    for it in unique:
        it["excerpt"] = truncate(it.get("summary", ""), chars)
        it["date_obj"] = parse_date(it.get("date"))
    return unique[:limit]


# --------------------------------------------------------------------------
# imagery — real photo if provided, otherwise an on-brand gradient stand-in
# --------------------------------------------------------------------------

# Curated nature-toned gradient pairs (top-left -> bottom-right). On-brand,
# never clashing. A photo dropped into static/images/ overrides these.
GRADIENTS = [
    ("#2e5c60", "#0e1c1e"),  # teal canopy
    ("#3c5f2c", "#131f12"),  # moss
    ("#8a5a1e", "#28190a"),  # warm ochre
    ("#2a5c50", "#0e201c"),  # deep green
    ("#2c4a68", "#101c28"),  # dusk blue
    ("#4a5a2e", "#1a2012"),  # olive
]


def gradient_for(slug):
    h = sum(ord(c) for c in slug)
    a, b = GRADIENTS[h % len(GRADIENTS)]
    return "linear-gradient(135deg, %s 0%%, %s 100%%)" % (a, b)


def image_style(post):
    """Inline background for a card/hero media area."""
    if post.get("image"):
        src = "/images/%s" % esc(post["image"])
        return "background-image:url('%s');background-size:cover;background-position:center;" % src
    return "background-image:%s;" % gradient_for(post["slug"])


def has_photo(post):
    return bool(post.get("image"))


def media_html(post, css_class, *, eager=False):
    """A real <img> when a photo exists (visible to search, has alt text, lazy
    loads below the fold); otherwise the on-brand gradient stand-in."""
    if has_photo(post):
        alt = esc(post.get("image_alt") or post["title"])
        load = 'loading="eager" fetchpriority="high"' if eager else 'loading="lazy"'
        return ('<span class="%s"><img class="media-img" src="/images/%s" alt="%s" '
                '%s decoding="async"></span>') % (css_class, esc(post["image"]), alt, load)
    return ('<span class="%s" style="background-image:%s" aria-hidden="true"></span>'
            % (css_class, gradient_for(post["slug"])))


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

def head(config, *, title, description, path, page_type="website",
         published=None, modified=None, noindex=False, jsonld=None, og_image=None,
         alt_path=None):
    base = config["base_url"].rstrip("/")
    url = base + urlp(path)
    full_title = title if title == config["site_name"] else "%s — %s" % (title, config["site_name"])
    robots = "noindex, follow" if noindex else "index, follow, max-image-preview:large, max-snippet:-1"
    share_image = og_image or "%s/og.%s" % (base, OG_EXT)
    default_share = share_image.endswith("/og.%s" % OG_EXT)

    tags = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>%s</title>" % esc(full_title),
        '<meta name="description" content="%s">' % esc(description),
        '<meta name="robots" content="%s">' % robots,
        '<link rel="canonical" href="%s">' % esc(url),
        '<link rel="icon" href="/favicon.svg" type="image/svg+xml">',
        '<link rel="icon" href="/favicon.ico" sizes="any">',
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">',
        '<meta name="theme-color" content="#101c17">',
        '<meta property="og:type" content="%s">' % page_type,
        '<meta property="og:title" content="%s">' % esc(full_title),
        '<meta property="og:description" content="%s">' % esc(description),
        '<meta property="og:url" content="%s">' % esc(url),
        '<meta property="og:site_name" content="%s">' % esc(config["site_name"]),
        '<meta property="og:locale" content="%s">' % esc(config.get("locale", "pt_PT")),
        '<meta property="og:image" content="%s">' % esc(share_image),
    ] + ([
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
    ] if default_share else []) + [
        '<meta name="twitter:card" content="summary_large_image">',
        '<meta name="twitter:title" content="%s">' % esc(full_title),
        '<meta name="twitter:description" content="%s">' % esc(description),
        '<meta name="twitter:image" content="%s">' % esc(share_image),
        '<link rel="alternate" type="application/rss+xml" title="%s" href="%s/feed.xml">' % (
            esc(config["site_name"]), esc(base)),
        '<link rel="preconnect" href="https://fonts.googleapis.com">',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        'family=Archivo:wght@500;600;700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&'
        'family=IBM+Plex+Mono:wght@400;500&display=swap">',
        '<link rel="stylesheet" href="/style.css">',
    ]
    if published:
        tags.append('<meta property="article:published_time" content="%s">' % published.isoformat())
    if modified:
        tags.append('<meta property="article:modified_time" content="%s">' % modified.isoformat())
    if jsonld:
        tags.append('<script type="application/ld+json">%s</script>'
                    % json.dumps(jsonld, ensure_ascii=False))
    if alt_path is not None:
        for lang in LANGUAGES:
            u = base + (lang["prefix"] + "/" if alt_path == "/" else lang["prefix"] + alt_path)
            tags.append('<link rel="alternate" hreflang="%s" href="%s">' % (lang["htmllang"], esc(u)))
        default_lang = next(x for x in LANGUAGES if x.get("default"))
        du = base + (default_lang["prefix"] + "/" if alt_path == "/" else default_lang["prefix"] + alt_path)
        tags.append('<link rel="alternate" hreflang="x-default" href="%s">' % esc(du))
    return "\n  ".join(tags)


def layout(config, *, head_html, body, active="", theme="dark", alt_path=None, has_alt=True):
    def nav(href, label):
        cur = ' aria-current="page"' if active == label else ""
        return '<a href="%s"%s>%s</a>' % (urlp(href), cur, label)

    def lang_switch():
        parts = []
        for lang in LANGUAGES:
            if alt_path is not None and (has_alt or lang.get("default") or lang["code"] == L["code"]):
                target = lang["prefix"] + "/" if alt_path == "/" else lang["prefix"] + alt_path
            else:
                target = lang["prefix"] + "/"
            if lang["code"] == L["code"]:
                parts.append('<span class="lang-atual" aria-current="true">%s</span>' % lang["label"])
            else:
                parts.append('<a href="%s" hreflang="%s">%s</a>' % (
                    esc(target), lang["htmllang"], lang["label"]))
        return '<div class="linguas" aria-label="Language">%s</div>' % "".join(parts)

    return """<!doctype html>
<html lang="%(lang)s" class="theme-%(theme)s">
<head>
  %(head)s
</head>
<body>
  <a class="skip" href="#principal">%(skip)s</a>
  <header class="topo">
    <div class="linha">
      <a class="marca" href="%(homehref)s">
        <svg class="marca-icone" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="6.2" cy="9.4" r="2.5"/>
          <circle cx="10.6" cy="6.4" r="2.7"/>
          <circle cx="15.4" cy="6.4" r="2.7"/>
          <circle cx="19.8" cy="9.4" r="2.5"/>
          <path d="M12 10.4c3.2 0 5.9 2.2 5.9 4.9 0 2.1-1.9 3.1-3.4 3.1-1 0-1.7-.5-2.5-.5s-1.5.5-2.5.5c-1.5 0-3.4-1-3.4-3.1 0-2.7 2.7-4.9 5.9-4.9z"/>
        </svg>
        <span class="marca-textos">
          <span class="marca-nome">%(site)s</span>
          <span class="marca-sub">%(tagline)s</span>
        </span>
      </a>
      <nav aria-label="Main">
        %(nav)s
        %(langs)s
      </nav>
    </div>
  </header>
  <main id="principal">
%(body)s
  </main>
  <footer class="rodape">
    <div class="linha">
      <p>%(site)s — %(tagline)s</p>
      <p class="mudo">
        <a href="/feed.xml">RSS</a> ·
        <a href="/sitemap.xml">Sitemap</a> ·
        <a href="%(abouthref)s">%(about)s</a>
      </p>
      <p class="mudo">%(footer)s</p>
    </div>
  </footer>
</body>
</html>
""" % {
        "lang": L["htmllang"],
        "theme": theme,
        "head": head_html,
        "skip": esc(T("skip")),
        "homehref": urlp("/"),
        "abouthref": urlp("/about/"),
        "site": esc(config["site_name"]),
        "tagline": esc(T("tagline")),
        "nav": "\n        ".join(
            [nav("/", T("nav_home")), nav("/topics/", T("nav_topics")),
             nav("/radar/", T("nav_radar")), nav("/search/", T("nav_search")),
             nav("/about/", T("nav_about"))]
            if L["code"] == "en" else
            [nav("/", T("nav_home")), nav("/about/", T("nav_about"))]),
        "langs": lang_switch(),
        "about": esc(T("nav_about")),
        "footer": esc(T("footer_note")),
        "body": body,
    }


def card(post):
    tag = esc(post["tags"][0]) if post["tags"] else ""
    tag_html = '<span class="cartao-tag">%s</span>' % tag if tag else ""
    return """      <article class="cartao">
        <a class="cartao-liga" href="%(href)s">
          %(media)s
          <div class="cartao-corpo">
            %(tag)s
            <h3>%(title)s</h3>
            <p class="meta">
              <time datetime="%(iso)s">%(data)s</time>
              <span aria-hidden="true">·</span>
              <span>%(rt)s</span>
            </p>
          </div>
        </a>
      </article>""" % {
        "href": urlp("/articles/%s/" % post["slug"]),
        "title": esc(post["title"]),
        "media": media_html(post, "cartao-media"),
        "tag": tag_html,
        "iso": post["date"].isoformat() if post["date"] else "",
        "data": pt_date(post["date"]),
        "rt": T("min_read", post["read_time"]),
    }


def radar_row(item):
    dt = item.get("date_obj")
    host = re.sub(r"^www\.", "", (item["link"].split("/")[2] if "://" in item["link"] else ""))
    return """      <li class="radar-item">
        <a class="radar-liga" href="%(link)s" rel="nofollow noopener external" target="_blank">
          <span class="radar-titulo">%(title)s</span>
          <svg class="seta" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <path d="M4 12L12 4M12 4H6M12 4v6" fill="none" stroke="currentColor"
                  stroke-width="1.5" stroke-linecap="square"/>
          </svg>
        </a>
        <p class="radar-resumo">%(excerpt)s</p>
        <p class="radar-meta">
          <span class="fonte">%(source)s</span>
          <span class="mudo">%(host)s</span>
          %(date)s
        </p>
      </li>""" % {
        "link": esc(item["link"]),
        "title": esc(item["title"]),
        "excerpt": esc(item.get("excerpt", "")),
        "source": esc(item["source"]),
        "host": esc(host),
        "date": '<time datetime="%s">%s</time>' % (dt.isoformat(), pt_date(dt)) if dt else "",
    }


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_home(config, posts, radar):
    base = config["base_url"].rstrip("/")
    home_url = base + urlp("/")
    jsonld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "@id": home_url + "#website", "url": home_url,
             "name": config["site_name"], "description": config["description"],
             "inLanguage": L["htmllang"]},
            {"@type": "Organization", "@id": home_url + "#org", "name": config["site_name"],
             "url": home_url},
        ],
    }
    destaque = posts[0] if posts else None
    featured = posts[1:3]
    resto = posts[3:]

    hero = ""
    if destaque:
        hero = """    <section class="hero">
      <a class="hero-liga" href="%(href)s">
        %(media)s
        <div class="hero-texto">
          <p class="sobrancelha">%(latest)s</p>
          <h1>%(title)s</h1>
          <p class="chamada">%(desc)s</p>
          <p class="meta">
            <time datetime="%(iso)s">%(data)s</time>
            <span aria-hidden="true">·</span>
            <span>%(rt)s</span>
          </p>
        </div>
      </a>
    </section>""" % {
            "href": urlp("/articles/%s/" % destaque["slug"]), "title": esc(destaque["title"]),
            "desc": esc(destaque["description"]), "latest": esc(T("latest")),
            "media": media_html(destaque, "hero-media", eager=True),
            "iso": destaque["date"].isoformat() if destaque["date"] else "",
            "data": pt_date(destaque["date"]), "rt": T("min_read", destaque["read_time"]),
        }

    grelha = ""
    if featured or resto:
        blocos = []
        if featured:
            blocos.append('      <div class="destaques">\n%s\n      </div>'
                          % "\n".join(card(p) for p in featured))
        if resto:
            blocos.append('      <div class="grelha">\n%s\n      </div>'
                          % "\n".join(card(p) for p in resto))
        grelha = """    <section class="seccao" aria-labelledby="more-articles">
      <h2 id="more-articles" class="seccao-titulo">%s</h2>
%s
    </section>""" % (esc(T("written_here")), "\n".join(blocos))

    radar_bloco = ""
    if L["code"] == "en":  # the Radar is English-only for now
        radar_bloco = """    <section class="seccao radar" aria-labelledby="radar-titulo">
      <div class="seccao-cabeca">
        <h2 id="radar-titulo" class="seccao-titulo">%(titulo)s</h2>
        <p class="seccao-nota">%(nota)s</p>
      </div>
      <ul class="radar-lista">
%(itens)s
      </ul>
      <p class="mais"><a href="%(href)s">%(ver)s</a></p>
    </section>""" % {
            "titulo": esc(T("radar_title")), "nota": esc(T("radar_home_note")),
            "itens": "\n".join(radar_row(i) for i in radar[:6]),
            "href": urlp("/radar/"), "ver": esc(T("see_full_radar")),
        }

    body = "\n".join(x for x in [hero, grelha, radar_bloco] if x)
    head_html = head(config, title=config["site_name"], description=config["description"],
                     path="/", jsonld=jsonld, alt_path="/")
    write(outp("index.html"),
          layout(config, head_html=head_html, body=body, active=T("nav_home"),
                 alt_path="/", has_alt=True))


def build_post(config, post, posts, topic_tags=frozenset()):
    base = config["base_url"].rstrip("/")
    url = base + urlp("/articles/%s/" % post["slug"])
    jsonld = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post["title"],
        "description": post["description"],
        "url": url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "inLanguage": L["htmllang"],
        "author": {"@type": "Person" if post.get("author") else "Organization",
                   "name": post.get("author") or config["author"]},
        "publisher": {"@type": "Organization", "name": config["site_name"], "url": base + "/"},
        "wordCount": len(re.findall(r"\w+", post["plain"])),
    }
    if post["date"]:
        jsonld["datePublished"] = post["date"].isoformat()
    if post["updated"]:
        jsonld["dateModified"] = post["updated"].isoformat()
    if post["tags"]:
        jsonld["keywords"] = ", ".join(post["tags"])
    if has_photo(post):
        jsonld["image"] = "%s/images/%s" % (base, post["image"])

    crumbs = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": T("nav_home"), "item": base + urlp("/")},
            {"@type": "ListItem", "position": 2, "name": T("written_here"),
             "item": base + urlp("/#more-articles")},
            {"@type": "ListItem", "position": 3, "name": post["title"], "item": url},
        ],
    }

    toc = ""
    if len(post["toc"]) >= 3:
        toc = """      <nav class="indice" aria-label="%(rot)s">
        <p class="indice-titulo">%(rot)s</p>
        <ol>
%(itens)s
        </ol>
      </nav>""" % {"rot": esc(T("in_this_article")),
                   "itens": "\n".join('          <li><a href="#%s">%s</a></li>' % (esc(h), esc(t))
                                      for h, t in post["toc"])}

    # related by shared tags, then filled with most recent
    my_tags = set(post["tags"])
    scored = []
    for p in posts:
        if p["slug"] == post["slug"]:
            continue
        shared = len(my_tags & set(p["tags"]))
        scored.append((shared, p))
    scored.sort(key=lambda sp: (sp[0], sp[1]["date"] or datetime.min.replace(tzinfo=timezone.utc)),
                reverse=True)
    relacionados = [p for _, p in scored[:3]]
    rel = ""
    if relacionados:
        rel = """    <section class="seccao" aria-labelledby="read-next">
      <h2 id="read-next" class="seccao-titulo">%s</h2>
      <div class="grelha">
%s
      </div>
    </section>""" % (esc(T("read_next")), "\n".join(card(p) for p in relacionados))

    tags = ""
    if post["tags"]:
        def tag_li(t):
            if t in topic_tags:
                return '<li><a href="%s">%s</a></li>' % (urlp("/topics/%s/" % slugify(t)), esc(t))
            return '<li class="plain">%s</li>' % esc(t)
        tags = '<ul class="etiquetas">%s</ul>' % "".join(tag_li(t) for t in post["tags"])

    banner = ""
    if has_photo(post):
        credit = ""
        if post.get("image_credit"):
            credit = '<figcaption class="credito">%s</figcaption>' % esc(post["image_credit"])
        banner = ("""      <figure class="artigo-banner">
        %s
        %s
      </figure>""" % (media_html(post, "banner-media", eager=True), credit))

    body = """    <div class="progresso" id="progresso" aria-hidden="true"></div>
    <article class="artigo">
      <header class="artigo-cabeca">
        <p class="sobrancelha"><a href="%(homehref)s">%(site)s</a> / %(articleword)s</p>
        <h1>%(title)s</h1>
        <p class="chamada">%(desc)s</p>
        <p class="meta">
          <time datetime="%(iso)s">%(data)s</time>
          <span aria-hidden="true">·</span>
          <span>%(rt)s</span>
        </p>
        %(tags)s
      </header>
%(banner)s
%(toc)s
      <div class="prosa">
%(html)s
      </div>
      <footer class="partilha" aria-label="Share this article">
        <span class="partilha-rot">%(shareword)s</span>
        <a class="partilha-btn" href="https://twitter.com/intent/tweet?url=%(u)s&amp;text=%(t)s" target="_blank" rel="noopener">X</a>
        <a class="partilha-btn" href="https://www.facebook.com/sharer/sharer.php?u=%(u)s" target="_blank" rel="noopener">Facebook</a>
        <a class="partilha-btn" href="https://www.linkedin.com/sharing/share-offsite/?url=%(u)s" target="_blank" rel="noopener">LinkedIn</a>
        <a class="partilha-btn" href="mailto:?subject=%(t)s&amp;body=%(u)s">Email</a>
        <button class="partilha-btn" id="copiar" type="button">%(copyword)s</button>
      </footer>
    </article>
%(rel)s
    <script type="application/ld+json">%(crumbs)s</script>
    <script>
    (function () {
      var bar = document.getElementById('progresso');
      var de = document.documentElement;
      function prog() {
        var m = de.scrollHeight - de.clientHeight;
        var p = m > 0 ? de.scrollTop / m : 0;
        bar.style.transform = 'scaleX(' + Math.min(1, Math.max(0, p)) + ')';
      }
      document.addEventListener('scroll', prog, { passive: true });
      window.addEventListener('resize', prog); prog();

      var links = document.querySelectorAll('.indice a');
      if (links.length) {
        var secs = [];
        links.forEach(function (a) {
          var el = document.getElementById(a.getAttribute('href').slice(1));
          if (el) secs.push([a, el]);
        });
        function spy() {
          var y = window.scrollY + 140, cur = null;
          secs.forEach(function (pair) { if (pair[1].offsetTop <= y) cur = pair[0]; });
          links.forEach(function (a) { a.classList.toggle('ativo', a === cur); });
        }
        document.addEventListener('scroll', spy, { passive: true }); spy();
      }

      var cp = document.getElementById('copiar');
      if (cp && navigator.clipboard) {
        cp.addEventListener('click', function () {
          navigator.clipboard.writeText(location.href).then(function () {
            var o = cp.textContent; cp.textContent = '%(copiedword)s';
            setTimeout(function () { cp.textContent = o; }, 1500);
          });
        });
      } else if (cp) { cp.style.display = 'none'; }
    })();
    </script>""" % {
        "site": esc(config["site_name"]), "title": esc(post["title"]),
        "desc": esc(post["description"]),
        "homehref": urlp("/"), "articleword": esc(T("article")),
        "shareword": esc(T("share")), "copyword": esc(T("copy_link")),
        "copiedword": T("copied"),
        "iso": post["date"].isoformat() if post["date"] else "",
        "data": pt_date(post["date"]), "rt": T("min_read", post["read_time"]),
        "tags": tags, "banner": banner, "toc": toc, "html": post["html"], "rel": rel,
        "crumbs": json.dumps(crumbs, ensure_ascii=False),
        "u": urllib.parse.quote(url, safe=""),
        "t": urllib.parse.quote(post["title"], safe=""),
    }

    base_url = config["base_url"].rstrip("/")
    art_og = "%s/images/%s" % (base_url, post["image"]) if has_photo(post) else None
    alt = "/articles/%s/" % post["slug"]
    has_alt = True if L["code"] != "en" else (post["slug"] in PT_SLUGS)
    head_html = head(config, title=post["title"], description=post["description"],
                     path=alt, page_type="article",
                     published=post["date"], modified=post["updated"],
                     noindex=post["noindex"], jsonld=jsonld, og_image=art_og,
                     alt_path=(alt if has_alt else None))
    write(outp("articles", post["slug"], "index.html"),
          layout(config, head_html=head_html, body=body, theme="light",
                 alt_path=alt, has_alt=has_alt))


def build_radar(config, radar):
    """Aggregation page: noindex on purpose (see README)."""
    body = """    <section class="seccao radar radar-pagina" aria-labelledby="radar-titulo">
      <div class="seccao-cabeca">
        <h1 id="radar-titulo" class="seccao-titulo">Radar</h1>
        <p class="seccao-nota">
          A selection of what came out elsewhere. Title, a short excerpt and a link to the source —
          the full text stays where it was published. %d items, refreshed on every build.
        </p>
      </div>
      <ul class="radar-lista">
%s
      </ul>
      <p class="mudo fontes">Sources: %s.</p>
    </section>""" % (
        len(radar),
        "\n".join(radar_row(i) for i in radar),
        ", ".join(esc(f["name"]) for f in config.get("feeds", [])),
    )
    head_html = head(config, title="Radar",
                     description="A selection of articles published on other sites, linking to the source.",
                     path="/radar/", noindex=True)
    write(DIST / "radar" / "index.html", layout(config, head_html=head_html, body=body, active="Radar"))


def qualifying_topics(posts, minimum=2):
    """Tags with enough articles to deserve their own page (avoids thin pages)."""
    counts = {}
    for p in posts:
        if p["noindex"]:
            continue
        for t in p["tags"]:
            counts[t] = counts.get(t, 0) + 1
    return {t for t, n in counts.items() if n >= minimum}


def build_topics(config, posts, topic_tags):
    """One hub page per qualifying tag, plus a topics index."""
    tag_map = {}
    for p in posts:
        if p["noindex"]:
            continue
        for t in p["tags"]:
            if t in topic_tags:
                tag_map.setdefault(t, []).append(p)

    # index of all topics
    if tag_map:
        rows = []
        for tag in sorted(tag_map, key=lambda t: (-len(tag_map[t]), t.lower())):
            n = len(tag_map[tag])
            rows.append(
                '        <li><a href="/topics/%s/"><span class="topic-nome">%s</span>'
                '<span class="topic-conta">%d article%s</span></a></li>'
                % (slugify(tag), esc(tag), n, "" if n == 1 else "s"))
        body = """    <section class="seccao">
      <p class="sobrancelha">Browse</p>
      <h1 class="seccao-titulo">Topics</h1>
      <ul class="topic-lista">
%s
      </ul>
    </section>""" % "\n".join(rows)
        head_html = head(config, title="Topics",
                         description="Browse Fauna Report by topic — conservation, ecology, taxonomy and more.",
                         path="/topics/")
        write(DIST / "topics" / "index.html",
              layout(config, head_html=head_html, body=body, active="Topics"))

    # one page per tag
    for tag, items in tag_map.items():
        items = sorted(items, key=lambda d: d["date"] or datetime.min.replace(tzinfo=timezone.utc),
                       reverse=True)
        grid = "\n".join(card(p) for p in items)
        body = """    <section class="seccao">
      <p class="sobrancelha"><a href="/topics/">Topics</a></p>
      <h1 class="seccao-titulo">%s</h1>
      <p class="seccao-nota">%d article%s on this topic.</p>
      <div class="grelha" style="margin-top:2rem">
%s
      </div>
    </section>""" % (esc(tag), len(items), "" if len(items) == 1 else "s", grid)
        head_html = head(config, title="%s — Topics" % tag.capitalize(),
                         description="Fauna Report articles about %s." % tag,
                         path="/topics/%s/" % slugify(tag))
        write(DIST / "topics" / slugify(tag) / "index.html",
              layout(config, head_html=head_html, body=body, active="Topics"))

    return sorted(tag_map, key=lambda t: (-len(tag_map[t]), t.lower()))


def build_search(config, posts):
    """A JSON index + a tiny vanilla-JS search page (no libraries, no tracking)."""
    base = config["base_url"].rstrip("/")
    index = [{
        "title": p["title"],
        "url": "/articles/%s/" % p["slug"],
        "desc": p["description"],
        "tags": p["tags"],
        "date": pt_date(p["date"]),
    } for p in posts if not p["noindex"]]
    write(DIST / "search-index.json", json.dumps(index, ensure_ascii=False))

    body = """    <section class="seccao">
      <p class="sobrancelha">Find</p>
      <h1 class="seccao-titulo">Search</h1>
      <div class="busca">
        <input type="search" id="q" placeholder="Search articles\u2026"
               autocomplete="off" aria-label="Search articles">
        <p class="busca-nota" id="busca-nota">Type to search %d articles.</p>
        <ul class="busca-res" id="res"></ul>
      </div>
    </section>
    <script>
    (function () {
      var input = document.getElementById('q');
      var res = document.getElementById('res');
      var note = document.getElementById('busca-nota');
      var data = [];
      fetch('/search-index.json').then(function (r) { return r.json(); })
        .then(function (j) { data = j; var q = param(); if (q) { input.value = q; run(q); } })
        .catch(function () { note.textContent = 'Search is unavailable right now.'; });
      function param() {
        var m = location.search.match(/[?&]q=([^&]+)/);
        return m ? decodeURIComponent(m[1].replace(/\\+/g, ' ')) : '';
      }
      function esc(s) { return s.replace(/[&<>"]/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
      function run(q) {
        q = q.trim().toLowerCase();
        if (!q) { res.innerHTML = ''; note.textContent = 'Type to search ' + data.length + ' articles.'; return; }
        var hits = data.filter(function (a) {
          var hay = (a.title + ' ' + a.desc + ' ' + a.tags.join(' ')).toLowerCase();
          return q.split(/\\s+/).every(function (w) { return hay.indexOf(w) !== -1; });
        });
        note.textContent = hits.length + ' result' + (hits.length === 1 ? '' : 's') + ' for \u201c' + q + '\u201d';
        res.innerHTML = hits.map(function (a) {
          return '<li><a href="' + a.url + '"><span class="busca-titulo">' + esc(a.title) +
                 '</span><span class="busca-desc">' + esc(a.desc) + '</span></a></li>';
        }).join('');
      }
      input.addEventListener('input', function () { run(input.value); });
    })();
    </script>""" % len(index)
    head_html = head(config, title="Search",
                     description="Search Fauna Report articles.",
                     path="/search/", noindex=True)
    write(DIST / "search" / "index.html",
          layout(config, head_html=head_html, body=body, active="Search"))


def build_page(config, page):
    body = """    <article class="artigo">
      <header class="artigo-cabeca">
        <p class="sobrancelha"><a href="%(homehref)s">%(site)s</a> / %(pageword)s</p>
        <h1>%(title)s</h1>
      </header>
      <div class="prosa">
%(html)s
      </div>
    </article>""" % {"site": esc(config["site_name"]), "title": esc(page["title"]),
                     "homehref": urlp("/"), "pageword": esc(T("page")), "html": page["html"]}
    alt = "/%s/" % page["slug"]
    head_html = head(config, title=page["title"], description=page["description"],
                     path=alt, noindex=page["noindex"], alt_path=alt)
    write(outp(page["slug"], "index.html"),
          layout(config, head_html=head_html, body=body, theme="light",
                 active=T("nav_about") if page["slug"] == "about" else "",
                 alt_path=alt, has_alt=True))


def build_404(config):
    body = """    <section class="hero">
      <p class="sobrancelha">%(eyebrow)s</p>
      <h1>%(title)s</h1>
      <p class="chamada">%(body)s</p>
    </section>""" % {
        "eyebrow": esc(T("e404_eyebrow")), "title": esc(T("e404_title")),
        "body": T("e404_body") % (urlp("").rstrip("/") or "", urlp("").rstrip("/") or ""),
    }
    head_html = head(config, title=T("not_found_title"),
                     description=T("not_found_desc"), path="/404.html", noindex=True)
    write(outp("404.html"), layout(config, head_html=head_html, body=body))


# --------------------------------------------------------------------------
# SEO artefacts
# --------------------------------------------------------------------------

def build_sitemap(config, site_langs):
    base = config["base_url"].rstrip("/")
    now = datetime.now(timezone.utc)
    entries = []
    for sl in site_langs:
        pfx = sl["prefix"]
        entries.append((base + pfx + "/", now, "1.0", "daily"))
        for p in sl["posts"]:
            if p["noindex"]:
                continue
            entries.append(("%s%s/articles/%s/" % (base, pfx, p["slug"]),
                            p["updated"] or p["date"] or now, "0.8", "monthly"))
        if sl.get("topics"):
            entries.append(("%s%s/topics/" % (base, pfx), now, "0.5", "weekly"))
            for tag in sl["topics"]:
                entries.append(("%s%s/topics/%s/" % (base, pfx, slugify(tag)), now, "0.5", "weekly"))
        for p in sl["pages"]:
            if p["noindex"]:
                continue
            entries.append(("%s%s/%s/" % (base, pfx, p["slug"]),
                            p["updated"] or now, "0.4", "yearly"))

    rows = "\n".join(
        "  <url>\n    <loc>%s</loc>\n    <lastmod>%s</lastmod>\n"
        "    <changefreq>%s</changefreq>\n    <priority>%s</priority>\n  </url>"
        % (esc(u), d.date().isoformat(), cf, pr) for u, d, pr, cf in entries)

    write(DIST / "sitemap.xml",
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s\n</urlset>\n' % rows)


def build_robots(config):
    base = config["base_url"].rstrip("/")
    write(DIST / "robots.txt",
          "User-agent: *\n"
          "Allow: /\n"
          "Disallow: /radar/\n"
          "\n"
          "Sitemap: %s/sitemap.xml\n" % base)


def build_feed(config, posts):
    base = config["base_url"].rstrip("/")
    now = format_datetime(datetime.now(timezone.utc))
    items = []
    for p in posts[:20]:
        if p["noindex"]:
            continue
        url = "%s/articles/%s/" % (base, p["slug"])
        items.append(
            "    <item>\n"
            "      <title>%s</title>\n"
            "      <link>%s</link>\n"
            "      <guid isPermaLink=\"true\">%s</guid>\n"
            "      <description>%s</description>\n"
            "      %s\n"
            "    </item>" % (
                esc(p["title"]), esc(url), esc(url), esc(p["description"]),
                "<pubDate>%s</pubDate>" % format_datetime(p["date"]) if p["date"] else ""))

    write(DIST / "feed.xml",
          '<?xml version="1.0" encoding="UTF-8"?>\n'
          '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
          '  <channel>\n'
          '    <title>%s</title>\n'
          '    <link>%s/</link>\n'
          '    <description>%s</description>\n'
          '    <language>%s</language>\n'
          '    <lastBuildDate>%s</lastBuildDate>\n'
          '    <atom:link href="%s/feed.xml" rel="self" type="application/rss+xml"/>\n'
          '%s\n'
          '  </channel>\n'
          '</rss>\n' % (esc(config["site_name"]), esc(base), esc(config["description"]),
                        config.get("lang", "pt-PT"), now, esc(base), "\n".join(items)))


def build_og_image(config):
    """Share image as PNG (widely supported by social platforms).

    If static/og.png already exists it is used as-is (copied by copy_static),
    which guarantees the PNG on any build host regardless of Pillow/fonts.
    Otherwise it is generated with Pillow, falling back to SVG if unavailable.
    """
    global OG_EXT

    if (STATIC / "og.png").exists():
        OG_EXT = "png"  # copy_static() will place it at dist/og.png
        return

    name = config["site_name"]
    tagline = config["tagline"]
    domain = config["base_url"].replace("https://", "").replace("http://", "").rstrip("/")

    try:
        from PIL import Image, ImageDraw, ImageFont

        fd = "/usr/share/fonts/truetype/dejavu"
        f_bold = ImageFont.truetype("%s/DejaVuSans-Bold.ttf" % fd, 84)
        f_body = ImageFont.truetype("%s/DejaVuSerif.ttf" % fd, 34)
        f_mono = ImageFont.truetype("%s/DejaVuSansMono.ttf" % fd, 26)

        img = Image.new("RGB", (1200, 630), "#101c17")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 1200, 10], fill="#5fb27a")

        def wrap(text, font, max_w):
            words, lines, line = text.split(), [], ""
            for w in words:
                trial = (line + " " + w).strip()
                if d.textlength(trial, font=font) <= max_w:
                    line = trial
                else:
                    if line:
                        lines.append(line)
                    line = w
            if line:
                lines.append(line)
            return lines

        y = 250
        for ln in wrap(name, f_bold, 1040):
            d.text((80, y), ln, font=f_bold, fill="#f1eee2")
            y += 96
        y += 6
        for ln in wrap(tagline, f_body, 1040)[:2]:
            d.text((80, y), ln, font=f_body, fill="#9AA6B2")
            y += 46
        d.text((80, 560), domain, font=f_mono, fill="#5fb27a")

        img.save(DIST / "og.png", "PNG")
        OG_EXT = "png"
        return
    except Exception as exc:
        print("  ! og.png fallback to svg (%s)" % exc)

    svg = ("<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"1200\" height=\"630\" "
           "viewBox=\"0 0 1200 630\">\n"
           "  <rect width=\"1200\" height=\"630\" fill=\"#101c17\"/>\n"
           "  <rect x=\"0\" y=\"0\" width=\"1200\" height=\"10\" fill=\"#5fb27a\"/>\n"
           "  <text x=\"80\" y=\"300\" font-family=\"Helvetica, sans-serif\" font-size=\"86\" "
           "font-weight=\"700\" fill=\"#F4F6F8\">%s</text>\n"
           "  <text x=\"80\" y=\"372\" font-family=\"Georgia, serif\" font-size=\"34\" "
           "fill=\"#9AA6B2\">%s</text>\n"
           "  <text x=\"80\" y=\"560\" font-family=\"monospace\" font-size=\"24\" "
           "fill=\"#8EA4FF\">%s</text>\n</svg>\n") % (esc(name), esc(tagline), esc(domain))
    write(DIST / "og.svg", svg)
    OG_EXT = "svg"


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def copy_static():
    if not STATIC.exists():
        return
    for src in STATIC.rglob("*"):
        if src.is_file():
            dst = DIST / src.relative_to(STATIC)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser(description="Fauna Report static site generator")
    ap.add_argument("--offline", action="store_true", help="do not hit the network; use the feed cache")
    ap.add_argument("--serve", action="store_true", help="serve dist/ at http://localhost:8000")
    args = ap.parse_args()

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

    print("Cleaning dist/")
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    def plural(n, word):
        return "%d %s" % (n, word if n == 1 else word + "s")

    print("Reading own content")
    content_by_lang = {}
    for lang in LANGUAGES:
        lp = load_documents(CONTENT / lang["posts"])
        lg = load_documents(CONTENT / lang["pages"])
        content_by_lang[lang["code"]] = (lp, lg)
        print("  %s: %s, %s" % (lang["code"], plural(len(lp), "article"), plural(len(lg), "page")))

    global PT_SLUGS
    PT_SLUGS = {p["slug"] for p in content_by_lang.get("pt", ([], []))[0]}

    print("Collecting external feeds")
    radar = collect_radar(config, offline=args.offline)
    print("  %s in the radar" % plural(len(radar), "item"))

    # Build the share image first so OG_EXT is set before pages reference it.
    build_og_image(config)

    global L
    print("Generating pages")
    site_langs = []
    for lang in LANGUAGES:
        L = {**lang, "strings": STRINGS[lang["code"]]}
        posts, pages = content_by_lang[lang["code"]]
        topic_tags = qualifying_topics(posts)
        topics = None
        build_home(config, posts, radar)
        for p in posts:
            build_post(config, p, posts, topic_tags)
        for p in pages:
            build_page(config, p)
        if lang.get("default"):        # English-only sections for now
            topics = build_topics(config, posts, topic_tags)
            build_search(config, posts)
            build_radar(config, radar)
            build_404(config)
        site_langs.append({"prefix": lang["prefix"], "posts": posts,
                           "pages": pages, "topics": topics})

    # SEO artefacts (shared, from the default language context)
    L = {**LANGUAGES[0], "strings": STRINGS[LANGUAGES[0]["code"]]}
    print("Generating SEO artefacts")
    build_sitemap(config, site_langs)
    build_robots(config)
    build_feed(config, content_by_lang[LANGUAGES[0]["code"]][0])

    copy_static()

    total = sum(1 for _ in DIST.rglob("*") if _.is_file())
    print("Done: %d files in dist/" % total)

    if args.serve:
        import http.server
        import socketserver
        os.chdir(DIST)
        handler = http.server.SimpleHTTPRequestHandler
        with socketserver.TCPServer(("", 8000), handler) as httpd:
            print("Serving at http://localhost:8000  (Ctrl+C to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.")


if __name__ == "__main__":
    sys.exit(main())
