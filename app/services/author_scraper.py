from __future__ import annotations
import re
import time
import random
import html as html_module
from typing import Optional
from bs4 import BeautifulSoup
from .story_downloader import get_session
from .logger import log_action, log_error

_STORY_SLUG_RE = re.compile(r"""url:["']([a-z0-9][a-z0-9-]{4,80})["']""")

_DATE_RE = re.compile(r"""date_approve["']?\s*:\s*["']([^"',}\s]{5,30})["']""")
_DESC_RE = re.compile(r'description\s*:\s*"((?:[^"\\]|\\.)*)"')


def normalize_author_url(url: str) -> Optional[str]:
    url = url.strip()

    m = re.match(r'https?://(?:www\.)?literotica\.com/authors/([^/?#]+)', url)
    if m:
        return f'https://www.literotica.com/authors/{m.group(1)}'

    m = re.match(r'https?://(?:www\.)?literotica\.com/stories/memberpage\.php\?uid=(\d+)', url)
    if m:
        return f'https://www.literotica.com/stories/memberpage.php?uid={m.group(1)}&page=submissions'

    return None


def is_author_url(url: str) -> bool:
    """Return True if the URL looks like a Literotica author page."""
    return bool(
        re.search(r'literotica\.com/authors/', url) or
        re.search(r'literotica\.com/stories/memberpage\.php\?uid=', url)
    )


def _latest_date(dates: list[str]) -> str:
    """Return the most recent date from a list of MM/DD/YYYY strings."""
    def sort_key(d: str):
        try:
            m, day, y = d.split('/')
            return (int(y), int(m), int(day))
        except Exception:
            return (0, 0, 0)
    return max(dates, key=sort_key)


def _has_class_prefix(tag, prefix: str) -> bool:
    """True if any of the tag's CSS classes contains the given prefix string."""
    cls = tag.get('class', [])
    if isinstance(cls, str):
        cls = cls.split()
    return any(prefix in c for c in cls)


LITEROTICA_API = 'https://literotica.com/api/3'
WORKS_PAGE_SIZE = 50
WORKS_MAX_PAGES = 40   # 2,000 stories and series; nobody has more


def author_username(author_url: str) -> Optional[str]:
    """The username in a /authors/<name> URL (the works feed needs it); None for memberpage URLs."""
    m = re.match(r'https?://(?:www\.)?literotica\.com/authors/([^/?#]+)', (author_url or '').strip())
    return m.group(1) if m else None


def _category_name(info: Optional[dict]) -> Optional[str]:
    if not isinstance(info, dict):
        return None
    if info.get('name'):
        return info['name']
    page = info.get('pageUrl')
    if not page:
        return None
    from .category_scraper import CATEGORIES
    for slug, name, _ in CATEGORIES:
        if slug == page:
            return name
    return page.replace('-', ' ').title()


def _number(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _work_from_api(work: dict) -> dict:
    """One story from the works feed, in the shape the author window uses."""
    rating = _number(work.get('rate_all'))
    return {
        'url': f"https://www.literotica.com/s/{work.get('url', '')}",
        'title': html_module.unescape(work.get('title') or 'Untitled'),
        'is_series': False,
        'score': f'{rating:.2f}' if rating else None,
        'rating': rating,
        'views': work.get('view_count'),
        'favorites': work.get('favorite_count'),
        'comments': work.get('comment_count'),
        'saves': work.get('reading_lists_count'),
        'words': work.get('words_count'),
        'date_approve': work.get('date_approve'),
        'category': _category_name(work.get('category_info')),
        'description': html_module.unescape(work.get('description') or '') or None,
        'tags': [t.get('tag') for t in (work.get('tags') or []) if isinstance(t, dict) and t.get('tag')],
    }


def _series_from_api(series: dict) -> dict:
    """A series from the works feed, with its parts in order and totals for sorting."""
    parts = [_work_from_api(p) for p in (series.get('parts') or [])]
    ratings = [p['rating'] for p in parts if p['rating']]
    dates = [p['date_approve'] for p in parts if p.get('date_approve')]
    categories = list(dict.fromkeys(p['category'] for p in parts if p.get('category')))

    def total(key):
        values = [p[key] for p in parts if isinstance(p.get(key), (int, float))]
        return sum(values) if values else None

    rating = sum(ratings) / len(ratings) if ratings else None
    return {
        'url': f"https://www.literotica.com/series/se/{series.get('id')}",
        'title': html_module.unescape(series.get('title') or 'Untitled series'),
        'is_series': True,
        'chapter_count': len(parts) or series.get('work_count'),
        'parts': parts,
        'score': f'{rating:.2f}' if rating else None,
        'rating': rating,
        'views': total('views'),
        'favorites': total('favorites'),
        'comments': total('comments'),
        'saves': total('saves'),
        'words': total('words'),
        'date_approve': _latest_date(dates) if dates else None,
        'category': categories[0] if len(categories) == 1 else ('Multiple categories' if categories else None),
        'categories': categories,
        'description': html_module.unescape(series.get('description') or '') or next(
            (p['description'] for p in parts if p.get('description')), None),
        'tags': list(dict.fromkeys(t for p in parts for t in p['tags'])),
    }


def fetch_author_works(username: str, session=None) -> Optional[list[dict]]:
    """Every story and series of an author from Literotica's own works feed (the one its author
    pages use), all pages of it. Series come with their parts. Returns None if the feed can't be
    read, so the caller can fall back to reading the web page."""
    import json
    from urllib.parse import quote
    session = session or get_session()
    entries: list[dict] = []
    page, last_page = 1, 1
    while page <= min(last_page, WORKS_MAX_PAGES):
        params = json.dumps({'page': page, 'pageSize': WORKS_PAGE_SIZE, 'type': 'story', 'listType': 'expanded'},
                            separators=(',', ':'))
        url = f'{LITEROTICA_API}/users/{quote(username)}/series_and_works?params={quote(params)}'
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log_error(f"[AuthorScraper] Works feed page {page} for {username} failed: {e}")
            return None if page == 1 else entries
        if not isinstance(data, dict) or not isinstance(data.get('data'), list):
            log_error(f"[AuthorScraper] Works feed for {username} returned an unexpected reply")
            return None if page == 1 else entries
        for item in data['data']:
            if not isinstance(item, dict):
                continue
            entries.append(_series_from_api(item) if 'parts' in item else _work_from_api(item))
        last_page = int(data.get('last_page') or 1)
        page += 1
        if page <= last_page:
            time.sleep(random.uniform(0.5, 1.5))
    log_action(f"[AuthorScraper] Works feed for {username}: {len(entries)} entries "
               f"({sum(1 for e in entries if e['is_series'])} series)")
    return entries


class AuthorScraper:
    """Scrape an author's Literotica profile to discover their stories/series."""

    def scrape_story_list_with_metadata(self, author_url: str, skip_jitter: bool = False) -> list[dict]:
        """
        Fetch an author's works page and return a deduplicated list of story/series
        dicts enriched with score and date_approve from the SolidJS hydration data.

        Series are detected via Literotica's semantic DOM classes and consolidated into
        a single entry with avg score and the most recent chapter date.

        Each dict has:
            url (str), title (str), is_series (bool),
            score (str | None), date_approve (str | None),
            chapter_count (int | None)  — present on series entries only
        """
        canonical = normalize_author_url(author_url)
        if not canonical:
            log_error(f"Cannot normalise author URL: {author_url}")
            return []

        log_action(f"[AuthorScraper] Preview scrape for: {canonical}")

        if not skip_jitter:
            jitter = random.randint(5, 15)
            log_action(f"[AuthorScraper] Waiting {jitter}s before fetching author page")
            time.sleep(jitter)

        session = get_session()
        try:
            is_memberpage = 'memberpage.php' in canonical
            if is_memberpage:
                resp = session.get(canonical, timeout=20)
            else:
                works_url = canonical.rstrip('/') + '/works'
                resp = session.get(works_url, timeout=20)
                if resp.status_code == 404:
                    resp = session.get(canonical, timeout=20)
            resp.raise_for_status()
            html_text = resp.text
        except Exception as e:
            log_error(f"[AuthorScraper] Error fetching page for preview: {e}")
            return []

        soup = BeautifulSoup(html_text, 'html.parser')
        results, series_to_chapters, series_titles = self._parse_works_html_impl(html_text)
        hydration_meta = self._extract_metadata_from_script_soup(soup)
        dom_meta = self._extract_dom_metadata(soup)

        # Attach metadata to every entry (chapters and standalones alike)
        for story in results:
            slug = story['url'].rstrip('/').rsplit('/', 1)[-1]
            hm = hydration_meta.get(slug, {})
            dm = dom_meta.get(slug, {})
            story['score'] = dm.get('score')
            story['category'] = dm.get('category')
            story['date_approve'] = hm.get('date_approve')
            story['description'] = hm.get('description')

        # --- Consolidation diagnostics ---
        log_action(f"[AuthorScraper] Raw results before consolidation ({len(results)} total):")
        for r in results:
            log_action(f"  [raw]  is_series={r['is_series']}  url={r['url']}  title={r['title']!r}")

        log_action(f"[AuthorScraper] DOM series groupings ({len(series_to_chapters)} series):")
        for s_url, ch_set in series_to_chapters.items():
            log_action(f"  [series-dom]  {s_url}  title={series_titles.get(s_url)!r}")
            for ch in sorted(ch_set):
                log_action(f"    [chapter]  {ch}")

        all_chapter_urls: set[str] = set()
        for ch_set in series_to_chapters.values():
            all_chapter_urls.update(ch_set)

        # URL→entry lookup for fast metadata retrieval when building series entries
        results_by_url: dict[str, dict] = {r['url']: r for r in results}

        # Build one consolidated series entry per DOM-detected series
        consolidated_series: list[dict] = []
        for series_url, ch_set in series_to_chapters.items():
            title = series_titles.get(series_url, 'Unknown Series')
            chapter_entries = [results_by_url[u] for u in ch_set if u in results_by_url]

            scores = [float(e['score']) for e in chapter_entries if e.get('score')]
            dates = [e['date_approve'] for e in chapter_entries if e.get('date_approve')]
            unique_cats = list(dict.fromkeys(e['category'] for e in chapter_entries if e.get('category')))
            first_desc = next((e['description'] for e in chapter_entries if e.get('description')), None)

            log_action(
                f"[AuthorScraper] Building series: {series_url!r} title={title!r} "
                f"chapters={len(ch_set)} metadata_found={len(chapter_entries)}"
            )

            consolidated_series.append({
                'url': series_url,
                'title': title,
                'is_series': True,
                'chapter_count': len(ch_set),
                'score': f"{sum(scores) / len(scores):.2f}" if scores else None,
                'date_approve': _latest_date(dates) if dates else None,
                'category': unique_cats[0] if len(unique_cats) == 1 else ('Multiple categories' if unique_cats else None),
                'description': first_desc,
            })

        consolidated_urls: set[str] = {s['url'] for s in consolidated_series}
        standalone: list[dict] = []
        for r in results:
            if r['url'] in all_chapter_urls:
                log_action(f"[AuthorScraper] Dedup: dropping {r['url']!r} — confirmed series chapter (DOM)")
            elif r['url'] in consolidated_urls:
                log_action(f"[AuthorScraper] Dedup: dropping {r['url']!r} — is a consolidated series URL")
            else:
                standalone.append(r)

        log_action(f"[AuthorScraper] Final output: {len(standalone)} standalone, {len(consolidated_series)} series")
        log_action("[AuthorScraper] Standalone entries:")
        for s in standalone:
            log_action(f"  [standalone]  {s['url']}  title={s['title']!r}")
        log_action("[AuthorScraper] Series entries:")
        for s in consolidated_series:
            log_action(f"  [series]  {s['url']}  title={s['title']!r}  chapters={s['chapter_count']}")

        return standalone + consolidated_series

    def _parse_series_from_dom(self, soup: BeautifulSoup) -> list[dict]:
        """
        Parse series structure directly from Literotica's semantic DOM classes.

        Each series on the page is rendered as two sibling elements inside a common
        container:
          - Header card (class prefix _series_expanded_header_card_): holds the
            /series/se/ link and series title
          - Chapters wrapper (class prefix _series_parts__wrapper_): lists all /s/
            chapter links as direct children

        We anchor on the header card and use find_next_sibling to locate its paired
        wrapper. Walking up from the wrapper to find the series link is unreliable
        because ancestor containers span the full page and return the wrong series link.

        Class names have a trailing hash suffix that may change on Literotica rebuilds;
        we match by the stable prefix portion only.

        Returns list of dicts: {url, title, chapter_urls: list[str]}
        """
        result = []
        seen_series: set[str] = set()

        for header in soup.find_all(lambda tag: _has_class_prefix(tag, '_series_expanded_header_card_')):
            series_link = header.find('a', href=lambda h: h and '/series/se/' in h)
            if not series_link:
                log_action(f"[AuthorScraper] _parse_series_from_dom: header card has no /series/se/ link — skipping")
                continue

            series_href = series_link['href'].split('?')[0]
            if not series_href.startswith('http'):
                series_href = 'https://www.literotica.com' + series_href

            if series_href in seen_series:
                continue
            seen_series.add(series_href)

            title = html_module.unescape(series_link.get_text(strip=True)) or 'Unknown Series'

            # The chapters wrapper is the next sibling of the header card
            wrapper = header.find_next_sibling(lambda tag: _has_class_prefix(tag, '_series_parts__wrapper_'))
            if not wrapper:
                log_action(f"[AuthorScraper] _parse_series_from_dom: no sibling wrapper found for {series_href!r} — series has no chapters listed")
                result.append({'url': series_href, 'title': title, 'chapter_urls': []})
                continue

            chapter_urls: list[str] = []
            seen_ch: set[str] = set()
            for ch_link in wrapper.find_all('a', href=lambda h: h and '/s/' in h):
                href = ch_link['href'].split('?')[0]
                if not href.startswith('http'):
                    href = 'https://www.literotica.com' + href
                if href not in seen_ch:
                    seen_ch.add(href)
                    chapter_urls.append(href)

            log_action(f"[AuthorScraper] DOM series: {series_href!r} title={title!r} chapters={len(chapter_urls)}")
            result.append({'url': series_href, 'title': title, 'chapter_urls': chapter_urls})

        return result

    def _extract_dom_metadata(self, soup: BeautifulSoup) -> dict[str, dict]:
        """
        Extract score and category for each story from the rendered DOM of the author
        works page. Literotica renders scores in <span class="_stats__text_..."> elements
        and categories as <a href="/c/..."> links. Walks up from each /s/ link to find
        the nearest container holding both.
        Returns dict mapping slug -> {score, category}.
        """
        result: dict[str, dict] = {}
        for link in soup.find_all('a', href=lambda h: h and '/s/' in (h or '')):
            href = link['href'].split('?')[0]
            if not href.startswith('http'):
                href = 'https://www.literotica.com' + href
            slug = href.rstrip('/').rsplit('/', 1)[-1]
            if slug in result:
                continue

            score: str | None = None
            category: str | None = None
            container = link.parent
            for _ in range(6):
                if container is None:
                    break
                if score is None:
                    for span in container.find_all('span', class_=lambda c: c and '_stats__text_' in str(c)):
                        text = span.get_text(strip=True)
                        try:
                            val = float(text)
                            if 1.0 <= val <= 5.0 and '.' in text:
                                score = text
                                break
                        except ValueError:
                            continue
                if category is None:
                    for a in container.find_all('a', href=lambda h: h and '/c/' in (h or '')):
                        cat = a.get_text(strip=True)
                        if cat:
                            category = cat
                            break
                if score is not None and category is not None:
                    break
                container = container.parent

            result[slug] = {'score': score, 'category': category}

        found_scores = sum(1 for v in result.values() if v['score'])
        found_cats = sum(1 for v in result.values() if v['category'])
        log_action(f"[AuthorScraper] DOM metadata: {found_scores}/{len(result)} scores, {found_cats}/{len(result)} categories")
        return result

    def _extract_metadata_from_script_soup(self, soup: BeautifulSoup) -> dict[str, dict]:
        """
        Parse date_approve and description for each story slug from the SolidJS hydration script.
        Returns a dict mapping slug -> {date_approve, description}.
        """
        scripts = soup.find_all('script', src=False)
        if not scripts:
            return {}

        script_text = max(scripts, key=lambda s: len(s.get_text())).get_text()
        positions = list(_STORY_SLUG_RE.finditer(script_text))
        if not positions:
            return {}

        result: dict[str, dict] = {}
        for i, m in enumerate(positions):
            slug = m.group(1)
            start = max(0, m.start() - 800)
            end = positions[i + 1].start() if i + 1 < len(positions) else min(len(script_text), m.end() + 2000)
            window = script_text[start:end]

            date_m = _DATE_RE.search(window)

            desc: str | None = None
            desc_m = _DESC_RE.search(window)
            if desc_m:
                raw = desc_m.group(1)
                raw = raw.replace('\\"', '"').replace("\\'", "'").replace('\\n', ' ').replace('\\r', '').replace('\\\\', '\\').strip()
                desc = raw[:200] if len(raw) > 200 else raw or None

            result[slug] = {
                'date_approve': date_m.group(1) if date_m else None,
                'description': desc,
            }

        found_dates = sum(1 for v in result.values() if v['date_approve'])
        log_action(f"[AuthorScraper] Hydration extraction: {found_dates}/{len(result)} dates found")

        return result

    def scrape_story_urls(self, author_url: str) -> list[dict]:
        """
        Fetch the author's profile page and return a deduplicated list of
        story/series dicts.

        Each dict has:
            url (str): story URL (for standalone stories) or series URL
            title (str): story title
            is_series (bool): True when this entry represents a series
        """
        canonical = normalize_author_url(author_url)
        if not canonical:
            log_error(f"Cannot normalise author URL: {author_url}")
            return []

        log_action(f"[AuthorScraper] Starting scrape for: {canonical}")

        jitter = random.randint(5, 15)
        log_action(f"[AuthorScraper] Waiting {jitter}s before fetching author page")
        time.sleep(jitter)

        session = get_session()
        stories = self._fetch_works_page(session, canonical)

        if not stories:
            log_action(f"[AuthorScraper] No stories found via works page, trying legacy memberpage")
            stories = self._fetch_memberpage(session, canonical)

        log_action(f"[AuthorScraper] Found {len(stories)} distinct story/series entries")
        return stories

    def _fetch_works_page(self, session, author_url: str) -> list[dict]:
        """Fetch the /authors/<username>/works page (modern Literotica)."""
        works_url = author_url.rstrip('/') + '/works'
        try:
            resp = session.get(works_url, timeout=15)
            if resp.status_code == 404:
                log_action(f"[AuthorScraper] works page 404, will try memberpage")
                return []
            resp.raise_for_status()
            results, _, _ = self._parse_works_html_impl(resp.text)
            return results
        except Exception as e:
            log_error(f"[AuthorScraper] Error fetching works page {works_url}: {e}")
            return []

    def _parse_works_html(self, html: str, **_) -> list[dict]:
        """Thin wrapper kept for callers that only need the story list."""
        results, _, _ = self._parse_works_html_impl(html)
        return results

    def _parse_works_html_impl(self, html: str) -> tuple[list[dict], dict[str, set[str]], dict[str, str]]:
        """
        Parse the author's works/submissions page.

        Returns:
            results            — all story entries including series chapter entries
            series_to_chapters — maps series URL -> set of chapter URLs (authoritative, from DOM)
            series_titles      — maps series URL -> display title
        """
        soup = BeautifulSoup(html, 'html.parser')
        results: list[dict] = []
        seen_series: set[str] = set()
        seen_story: set[str] = set()
        series_titles: dict[str, str] = {}

        # Phase 0 — hydration data: extract all story slugs present on the page
        # Titles here are derived from slugs (e.g. "my-story-1" → "My Story 1") and will be
        # overwritten by real anchor text in Phase 2.
        results_index: dict[str, int] = {}  # url -> index in results, for Phase 2 title correction
        scripts = soup.find_all('script', src=False)
        if scripts:
            biggest = max(scripts, key=lambda s: len(s.get_text()))
            script_text = biggest.get_text()
            for slug in _STORY_SLUG_RE.findall(script_text):
                clean = f'https://www.literotica.com/s/{slug}'
                if clean not in seen_story:
                    seen_story.add(clean)
                    results_index[clean] = len(results)
                    results.append({'url': clean, 'title': slug.replace('-', ' ').title(), 'is_series': False})
            log_action(f"[AuthorScraper] Phase 0 hydration: found {len(seen_story)} story slugs")

        # Phase 1 — DOM-based series detection using Literotica's semantic class names
        series_entries = self._parse_series_from_dom(soup)
        series_to_chapters: dict[str, set[str]] = {}
        series_chapter_urls: set[str] = set()

        for se in series_entries:
            s_url = se['url']
            ch_urls = se['chapter_urls']
            series_titles[s_url] = se['title']
            seen_series.add(s_url)
            series_to_chapters[s_url] = set(ch_urls)
            series_chapter_urls.update(ch_urls)

        log_action(
            f"[AuthorScraper] Phase 1 DOM: {len(series_entries)} series, "
            f"{len(series_chapter_urls)} total chapter URLs"
        )

        # Warn if /series/se/ links exist but DOM class detection found nothing
        if not series_entries and soup.find('a', href=lambda h: h and '/series/se/' in h):
            log_action(
                "[AuthorScraper] WARNING: /series/se/ links present but _series_parts__wrapper_ "
                "not detected — Literotica may have changed its markup"
            )

        # Phase 2 — collect any /series/se/ entries Phase 1 missed, and any
        # /s/ stories not already found in Phase 0.
        for link in soup.find_all('a', href=True):
            href = link['href']
            if not href.startswith('http'):
                href = 'https://www.literotica.com' + href

            if '/series/se/' in href:
                clean = href.split('?')[0]
                title_text = html_module.unescape(link.get_text(strip=True)) or 'Unknown Series'
                if clean not in series_titles:
                    series_titles[clean] = title_text
                if clean not in seen_series:
                    seen_series.add(clean)
                    results.append({'url': clean, 'title': title_text, 'is_series': True})

            elif '/s/' in href:
                clean = href.split('?')[0]
                if clean in series_chapter_urls:
                    continue
                title_text = html_module.unescape(link.get_text(strip=True)) or 'Unknown Story'
                if clean not in seen_story:
                    seen_story.add(clean)
                    results_index[clean] = len(results)
                    results.append({'url': clean, 'title': title_text, 'is_series': False})
                elif title_text and title_text != 'Unknown Story' and clean in results_index:
                    # Overwrite the slug-derived title from Phase 0 with the real DOM title
                    results[results_index[clean]]['title'] = title_text

        return results, series_to_chapters, series_titles

    def _fetch_memberpage(self, session, author_url: str) -> list[dict]:
        """Legacy memberpage scrape fallback."""
        try:
            resp = session.get(author_url, timeout=15)
            resp.raise_for_status()
            results, _, _ = self._parse_works_html_impl(resp.text)
            return results
        except Exception as e:
            log_error(f"[AuthorScraper] Error fetching memberpage {author_url}: {e}")
            return []
