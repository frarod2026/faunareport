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

USER_AGENT = "EveryAnimal/1.0 (+https://everyanimal.net; feed aggregator)"
FEED_TIMEOUT = 12
OG_EXT = "png"  # set by build_og_image(); referenced by head()


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


def pt_date(dt):
    if not dt:
        return ""
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


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

def head(config, *, title, description, path, page_type="website",
         published=None, modified=None, noindex=False, jsonld=None):
    base = config["base_url"].rstrip("/")
    url = base + path
    full_title = title if title == config["site_name"] else "%s — %s" % (title, config["site_name"])
    robots = "noindex, follow" if noindex else "index, follow, max-image-preview:large, max-snippet:-1"

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
        '<meta property="og:image" content="%s/og.%s">' % (esc(base), OG_EXT),
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
        '<meta name="twitter:title" content="%s">' % esc(full_title),
        '<meta name="twitter:description" content="%s">' % esc(description),
        '<meta name="twitter:image" content="%s/og.%s">' % (esc(base), OG_EXT),
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
    return "\n  ".join(tags)


def layout(config, *, head_html, body, active="", theme="dark"):
    def nav(href, label):
        cur = ' aria-current="page"' if active == label else ""
        return '<a href="%s"%s>%s</a>' % (href, cur, label)

    return """<!doctype html>
<html lang="%(lang)s" class="theme-%(theme)s">
<head>
  %(head)s
</head>
<body>
  <a class="skip" href="#principal">Skip to content</a>
  <header class="topo">
    <div class="linha">
      <a class="marca" href="/">
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
        <a href="/about/">About</a>
      </p>
      <p class="mudo">The articles here are written for this site. The Radar links to other people&#39;s work, hosted by them.</p>
    </div>
  </footer>
</body>
</html>
""" % {
        "lang": config.get("lang", "pt-PT"),
        "theme": theme,
        "head": head_html,
        "site": esc(config["site_name"]),
        "tagline": esc(config["tagline"]),
        "nav": "\n        ".join([nav("/", "Home"), nav("/radar/", "Radar"), nav("/about/", "About")]),
        "body": body,
    }


def card(post):
    tag = esc(post["tags"][0]) if post["tags"] else ""
    tag_html = '<span class="cartao-tag">%s</span>' % tag if tag else ""
    photo_cls = " has-photo" if has_photo(post) else ""
    alt = esc(post.get("image_alt") or post["title"])
    aria = ' role="img" aria-label="%s"' % alt if has_photo(post) else ' aria-hidden="true"'
    return """      <article class="cartao">
        <a class="cartao-liga" href="/articles/%(slug)s/">
          <span class="cartao-media%(pcls)s" style="%(style)s"%(aria)s></span>
          <div class="cartao-corpo">
            %(tag)s
            <h3>%(title)s</h3>
            <p class="meta">
              <time datetime="%(iso)s">%(data)s</time>
              <span aria-hidden="true">·</span>
              <span>%(rt)d min read</span>
            </p>
          </div>
        </a>
      </article>""" % {
        "slug": esc(post["slug"]),
        "title": esc(post["title"]),
        "style": image_style(post),
        "pcls": photo_cls,
        "aria": aria,
        "tag": tag_html,
        "iso": post["date"].isoformat() if post["date"] else "",
        "data": pt_date(post["date"]),
        "rt": post["read_time"],
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
    jsonld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "@id": base + "/#website", "url": base + "/",
             "name": config["site_name"], "description": config["description"],
             "inLanguage": config.get("lang", "pt-PT")},
            {"@type": "Organization", "@id": base + "/#org", "name": config["site_name"],
             "url": base + "/"},
        ],
    }
    destaque = posts[0] if posts else None
    featured = posts[1:3]
    resto = posts[3:]

    hero = ""
    if destaque:
        pcls = " has-photo" if has_photo(destaque) else ""
        alt = esc(destaque.get("image_alt") or destaque["title"])
        aria = ' role="img" aria-label="%s"' % alt if has_photo(destaque) else ' aria-hidden="true"'
        hero = """    <section class="hero">
      <a class="hero-liga" href="/articles/%(slug)s/">
        <span class="hero-media%(pcls)s" style="%(style)s"%(aria)s></span>
        <div class="hero-texto">
          <p class="sobrancelha">Latest</p>
          <h1>%(title)s</h1>
          <p class="chamada">%(desc)s</p>
          <p class="meta">
            <time datetime="%(iso)s">%(data)s</time>
            <span aria-hidden="true">·</span>
            <span>%(rt)d min read</span>
          </p>
        </div>
      </a>
    </section>""" % {
            "slug": esc(destaque["slug"]), "title": esc(destaque["title"]),
            "desc": esc(destaque["description"]), "style": image_style(destaque),
            "pcls": pcls, "aria": aria,
            "iso": destaque["date"].isoformat() if destaque["date"] else "",
            "data": pt_date(destaque["date"]), "rt": destaque["read_time"],
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
      <h2 id="more-articles" class="seccao-titulo">Written here</h2>
%s
    </section>""" % "\n".join(blocos)

    radar_bloco = """    <section class="seccao radar" aria-labelledby="radar-titulo">
      <div class="seccao-cabeca">
        <h2 id="radar-titulo" class="seccao-titulo">Radar</h2>
        <p class="seccao-nota">Published elsewhere. Links open on the original site.</p>
      </div>
      <ul class="radar-lista">
%s
      </ul>
      <p class="mais"><a href="/radar/">See the full radar</a></p>
    </section>""" % "\n".join(radar_row(i) for i in radar[:6])

    body = "\n".join(x for x in [hero, grelha, radar_bloco] if x)
    head_html = head(config, title=config["site_name"], description=config["description"],
                     path="/", jsonld=jsonld)
    write(DIST / "index.html", layout(config, head_html=head_html, body=body, active="Home"))


def build_post(config, post, posts):
    base = config["base_url"].rstrip("/")
    url = "%s/articles/%s/" % (base, post["slug"])
    jsonld = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post["title"],
        "description": post["description"],
        "url": url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "inLanguage": config.get("lang", "pt-PT"),
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

    crumbs = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Articles", "item": base + "/#more-articles"},
            {"@type": "ListItem", "position": 3, "name": post["title"], "item": url},
        ],
    }

    toc = ""
    if len(post["toc"]) >= 3:
        toc = """      <nav class="indice" aria-label="In this article">
        <p class="indice-titulo">In this article</p>
        <ol>
%s
        </ol>
      </nav>""" % "\n".join('          <li><a href="#%s">%s</a></li>' % (esc(h), esc(t))
                            for h, t in post["toc"])

    relacionados = [p for p in posts if p["slug"] != post["slug"]][:2]
    rel = ""
    if relacionados:
        rel = """    <section class="seccao" aria-labelledby="read-next">
      <h2 id="read-next" class="seccao-titulo">Read next</h2>
      <div class="grelha">
%s
      </div>
    </section>""" % "\n".join(card(p) for p in relacionados)

    tags = ""
    if post["tags"]:
        tags = '<ul class="etiquetas">%s</ul>' % "".join(
            "<li>%s</li>" % esc(t) for t in post["tags"])

    banner = ""
    if has_photo(post):
        alt = esc(post.get("image_alt") or post["title"])
        credit = ""
        if post.get("image_credit"):
            credit = '<figcaption class="credito">%s</figcaption>' % esc(post["image_credit"])
        banner = ("""      <figure class="artigo-banner">
        <span class="banner-media has-photo" style="%s" role="img" aria-label="%s"></span>
        %s
      </figure>""" % (image_style(post), alt, credit))

    body = """    <article class="artigo">
      <header class="artigo-cabeca">
        <p class="sobrancelha"><a href="/">%(site)s</a> / Article</p>
        <h1>%(title)s</h1>
        <p class="chamada">%(desc)s</p>
        <p class="meta">
          <time datetime="%(iso)s">%(data)s</time>
          <span aria-hidden="true">·</span>
          <span>%(rt)d min read</span>
        </p>
        %(tags)s
      </header>
%(banner)s
%(toc)s
      <div class="prosa">
%(html)s
      </div>
    </article>
%(rel)s
    <script type="application/ld+json">%(crumbs)s</script>""" % {
        "site": esc(config["site_name"]), "title": esc(post["title"]),
        "desc": esc(post["description"]),
        "iso": post["date"].isoformat() if post["date"] else "",
        "data": pt_date(post["date"]), "rt": post["read_time"],
        "tags": tags, "banner": banner, "toc": toc, "html": post["html"], "rel": rel,
        "crumbs": json.dumps(crumbs, ensure_ascii=False),
    }

    head_html = head(config, title=post["title"], description=post["description"],
                     path="/articles/%s/" % post["slug"], page_type="article",
                     published=post["date"], modified=post["updated"],
                     noindex=post["noindex"], jsonld=jsonld)
    write(DIST / "articles" / post["slug"] / "index.html",
          layout(config, head_html=head_html, body=body, theme="light"))


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


def build_page(config, page):
    body = """    <article class="artigo">
      <header class="artigo-cabeca">
        <p class="sobrancelha"><a href="/">%(site)s</a> / Page</p>
        <h1>%(title)s</h1>
      </header>
      <div class="prosa">
%(html)s
      </div>
    </article>""" % {"site": esc(config["site_name"]), "title": esc(page["title"]),
                     "html": page["html"]}
    head_html = head(config, title=page["title"], description=page["description"],
                     path="/%s/" % page["slug"], noindex=page["noindex"])
    write(DIST / page["slug"] / "index.html",
          layout(config, head_html=head_html, body=body, theme="light",
                 active="About" if page["slug"] == "about" else ""))


def build_404(config):
    body = """    <section class="hero">
      <p class="sobrancelha">Error 404</p>
      <h1>This page does not exist</h1>
      <p class="chamada">The address may have changed, or never existed.
      Start from the <a href="/">home page</a> or check the <a href="/radar/">Radar</a>.</p>
    </section>"""
    head_html = head(config, title="Page not found",
                     description="The page you asked for does not exist.", path="/404.html", noindex=True)
    write(DIST / "404.html", layout(config, head_html=head_html, body=body))


# --------------------------------------------------------------------------
# SEO artefacts
# --------------------------------------------------------------------------

def build_sitemap(config, posts, pages):
    base = config["base_url"].rstrip("/")
    entries = [(base + "/", datetime.now(timezone.utc), "1.0", "daily")]
    for p in posts:
        if p["noindex"]:
            continue
        entries.append(("%s/articles/%s/" % (base, p["slug"]),
                        p["updated"] or p["date"] or datetime.now(timezone.utc), "0.8", "monthly"))
    for p in pages:
        if p["noindex"]:
            continue
        entries.append(("%s/%s/" % (base, p["slug"]),
                        p["updated"] or datetime.now(timezone.utc), "0.4", "yearly"))

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
    posts = load_documents(CONTENT / "posts")
    pages = load_documents(CONTENT / "pages")
    print("  %s, %s" % (plural(len(posts), "article"), plural(len(pages), "page")))

    print("Collecting external feeds")
    radar = collect_radar(config, offline=args.offline)
    print("  %s in the radar" % plural(len(radar), "item"))

    # Build the share image first so OG_EXT is set before pages reference it.
    build_og_image(config)

    print("Generating pages")
    build_home(config, posts, radar)
    for p in posts:
        build_post(config, p, posts)
    for p in pages:
        build_page(config, p)
    build_radar(config, radar)
    build_404(config)

    print("Generating SEO artefacts")
    build_sitemap(config, posts, pages)
    build_robots(config)
    build_feed(config, posts)

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
