"""
PuroMMA slate-prep — topic-diverse article selection (task #486).

Replaces the "N most-recent articles" dispatch rule that over-indexed on legacy
fighters (2 of 5 Stage 2 reels were McGregor). Fetches the latest articles from
the PuroMMA WordPress REST API, runs ONE Gemini flash pre-filter over the titles
to pick a topic-diverse slate (Spain-connected + current fighters weighted up,
legacy names capped), assigns a content_type per pick, then runs an image
suitability check on the picked featured photos (catches the composite/split
photos that broke the Topuria-Tsarukyan and BKFC renders in Stage 2 — #469
finding rolled into #486).

Runs locally (C3PO) before dispatch — NOT in GitHub Actions. Stdlib only.

Usage:
    py scripts/select_slate.py [--site https://puromma.com] [--fetch 20]
                               [--select 5] [--out slate.json]

Env:  GEMINI_API_KEY  (same key as the render pipeline)

Output (stdout + optional --out): JSON list, ready to drive repository_dispatch:
    [{"wp_post_id": 5151, "title": "...", "image_url": "https://...",
      "content_type": "spain_identity", "reason": "...", "image_check": "ok"}]
"""

import argparse, base64, json, os, sys, urllib.request, urllib.error

GEMINI_KEY      = os.environ.get('GEMINI_API_KEY', '')
GEMINI_FLASH    = 'gemini-3-flash-preview'
GEMINI_API_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'

CONTENT_TYPES = ('news_hook', 'spain_identity', 'evergreen', 'fight_week')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

MAX_IMAGE_BYTES = 4 * 1024 * 1024   # skip image check above this — fail open


def http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def gemini_call(parts):
    if not GEMINI_KEY:
        raise RuntimeError('GEMINI_API_KEY not set')
    url = f'{GEMINI_API_BASE}/{GEMINI_FLASH}:generateContent?key={GEMINI_KEY}'
    payload = {
        'contents': [{'parts': parts}],
        'generationConfig': {'responseMimeType': 'application/json'},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())
    return json.loads(resp['candidates'][0]['content']['parts'][0]['text'])


def fetch_recent_posts(site, n):
    """Latest n published posts with featured image URL via WP REST API."""
    url = (f'{site.rstrip("/")}/wp-json/wp/v2/posts'
           f'?per_page={n}&_embed=wp:featuredmedia&orderby=date&order=desc')
    posts = []
    for p in http_json(url):
        image_url = ''
        media = (p.get('_embedded') or {}).get('wp:featuredmedia') or []
        if media and isinstance(media[0], dict):
            image_url = media[0].get('source_url', '')
        posts.append({
            'wp_post_id': p['id'],
            'title': p['title']['rendered'],
            'link': p.get('link', ''),
            'image_url': image_url,
        })
    return posts


def select_diverse(posts, n_select):
    """One Gemini pre-filter over titles → ranked diverse picks + content_type.

    Asks for n_select + 5 ranked picks so flagged images can be swapped from the
    bench without a second selection call (Stage 2 showed ~40% of WP featured
    photos can be composites/collages).
    """
    titles_block = '\n'.join(
        f'- id={p["wp_post_id"]}: {p["title"]}' for p in posts
    )
    n_ranked = min(n_select + 5, len(posts))
    prompt = f"""You are the slate curator for PuroMMA (puromma.com), a Spanish-language
MMA site for an audience in SPAIN. From the recent article titles below, rank the
{n_ranked} best candidates for short Instagram Reels. Selection rules, in priority order:

1. TOPIC DIVERSITY — no two picks about the same fighter or the same storyline.
   Mix divisions, organisations and themes across the slate.
2. SPAIN FIRST — articles about Spanish fighters, Spain-based or Spain-connected
   fighters (e.g. Ilia Topuria — Georgian-born, favourite of the Spanish public),
   or events with a Spain connection rank ABOVE otherwise-equal picks.
3. CURRENT OVER LEGACY — weight current/active fighters over legacy-era names
   (Conor McGregor, Anderson Silva, GSP, Velasquez, etc.) whose news cycle is
   repetitive. AT MOST ONE legacy-fighter pick in the whole ranking, and only if
   it clearly adds variety.
4. Assign each pick a content_type from exactly: {list(CONTENT_TYPES)}.
   spain_identity for Spain-angle pieces; fight_week only for upcoming-event
   previews; evergreen for durable analysis; news_hook for everything else.

Article titles:
{titles_block}

Return ONLY valid JSON: a list of {n_ranked} objects, best first, each with keys
"wp_post_id" (int, from the list above), "content_type" (string), and
"reason" (one short sentence in English explaining the pick and its rank)."""
    picks = gemini_call([{'text': prompt}])
    by_id = {p['wp_post_id']: p for p in posts}
    ranked = []
    for pick in picks:
        post = by_id.get(pick.get('wp_post_id'))
        if not post:
            continue
        ct = pick.get('content_type', 'news_hook')
        ranked.append({
            **post,
            'content_type': ct if ct in CONTENT_TYPES else 'news_hook',
            'reason': pick.get('reason', ''),
        })
    return ranked


def check_image(image_url):
    """Gemini look at one featured photo: usable as a 9:16 single-subject render
    source? Returns 'ok', 'composite', 'unusable', or 'unchecked' (fail-open)."""
    if not image_url:
        return 'unusable'
    try:
        req = urllib.request.Request(image_url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            img = r.read()
        if len(img) > MAX_IMAGE_BYTES:
            return 'unchecked'
        mime = 'image/png' if image_url.lower().endswith('.png') else 'image/jpeg'
        verdict = gemini_call([
            {'inlineData': {'mimeType': mime,
                            'data': base64.b64encode(img).decode()}},
            {'text': (
                'This photo will be cropped to a 9:16 vertical portrait of ONE '
                'person for an MMA Instagram Reel. Classify it. Return ONLY valid '
                'JSON: {"verdict": "..."} where verdict is exactly one of: '
                '"ok" (single clear subject, survives a vertical crop), '
                '"composite" (side-by-side/split/collage of two or more separate '
                'photos — a vertical crop would cut a person in half or keep only '
                'one side), '
                '"unusable" (no clear person, heavy graphics/text, or too '
                'low-quality to use).'
            )},
        ])
        v = verdict.get('verdict', 'unchecked')
        return v if v in ('ok', 'composite', 'unusable') else 'unchecked'
    except Exception as e:
        print(f'  image check failed for {image_url[:60]} ({e}) — fail-open',
              file=sys.stderr)
        return 'unchecked'


def build_slate(site, n_fetch, n_select):
    posts = fetch_recent_posts(site, n_fetch)
    if not posts:
        raise RuntimeError(f'No posts returned from {site}')
    print(f'Fetched {len(posts)} recent posts from {site}', file=sys.stderr)

    ranked = select_diverse(posts, n_select)
    print(f'Gemini ranked {len(ranked)} candidates', file=sys.stderr)

    # Walk the ranking, image-check each candidate, keep the first n_select that
    # pass. 'composite'/'unusable' are skipped (the Stage 2 failure mode);
    # 'unchecked' passes — image check never blocks a slate on its own outage.
    slate = []
    for cand in ranked:
        if len(slate) >= n_select:
            break
        verdict = check_image(cand['image_url'])
        cand['image_check'] = verdict
        print(f'  [{verdict}] #{cand["wp_post_id"]} ({cand["content_type"]}) '
              f'{cand["title"][:70]}', file=sys.stderr)
        if verdict in ('ok', 'unchecked'):
            slate.append(cand)
    if len(slate) < n_select:
        print(f'WARNING: only {len(slate)}/{n_select} picks passed the image '
              f'check — bench exhausted', file=sys.stderr)
    return slate


def main():
    ap = argparse.ArgumentParser(description='PuroMMA topic-diverse slate prep')
    ap.add_argument('--site', default='https://puromma.com')
    ap.add_argument('--fetch', type=int, default=20)
    ap.add_argument('--select', type=int, default=5)
    ap.add_argument('--out', default='')
    args = ap.parse_args()

    slate = build_slate(args.site, args.fetch, args.select)
    out_json = json.dumps(slate, ensure_ascii=False, indent=2)
    print(out_json)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(out_json)
        print(f'Slate written to {args.out}', file=sys.stderr)


if __name__ == '__main__':
    main()
