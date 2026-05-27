"""
PuroMMA Reels Render Pipeline — GitHub Actions edition
Reads JOB_PAYLOAD from environment, renders 3-beat MP4 with kinetic captions,
uploads to Cloudflare R2, optionally POSTs result to callback_url.

Stage 1 scope: news_hook only, music-bed audio, no n8n callback required.
"""

import base64, json, os, subprocess, sys, tempfile, time, struct, urllib.request, urllib.error

# ── Env / config ──────────────────────────────────────────────────────────────

GEMINI_KEY      = os.environ['GEMINI_API_KEY']
R2_ENDPOINT     = os.environ['R2_ENDPOINT']        # https://<acct>.r2.cloudflarestorage.com
R2_BUCKET       = os.environ['R2_BUCKET_NAME']     # playon-reels
R2_PUBLIC_URL   = os.environ['R2_PUBLIC_URL']      # https://pub-xxx.r2.dev
# R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY are used by aws-cli via env vars
# (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY — set in GH Actions env block)

GEMINI_FLASH    = 'gemini-3-flash-preview'
GEMINI_API_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'

# Font path (bundled in repo — Anton Regular, OFL license)
FONT_PATH = os.path.join(os.path.dirname(__file__), '..', 'assets', 'fonts', 'Anton-Regular.ttf')
FONT_PATH = os.path.realpath(FONT_PATH)

MUSIC_PATH = os.path.join(os.path.dirname(__file__), '..', 'assets', 'audio', 'bed.wav')
MUSIC_PATH = os.path.realpath(MUSIC_PATH)

LOGO_PATH = os.path.join(os.path.dirname(__file__), '..', 'assets', 'puromma_logo.png')
LOGO_PATH = os.path.realpath(LOGO_PATH)

FPS        = 25
TOTAL_DUR  = 9.0   # 3 beats × 3s each
BEAT_DUR   = 3.0
WORK_W     = 2160  # 2× output resolution for zoompan quality
WORK_H     = 3840

# ── Helpers ───────────────────────────────────────────────────────────────────

def gemini_json(prompt):
    url = f'{GEMINI_API_BASE}/{GEMINI_FLASH}:generateContent?key={GEMINI_KEY}'
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'responseMimeType': 'application/json'},
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    text = resp['candidates'][0]['content']['parts'][0]['text']
    return json.loads(text)


def audio_duration(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', path],
        capture_output=True, text=True, check=True
    )
    streams = json.loads(r.stdout)['streams']
    for s in streams:
        if s.get('codec_type') == 'audio':
            return float(s['duration'])
    raise RuntimeError(f'No audio stream in {path}')


def download_image(url, dest_path):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        with open(dest_path, 'wb') as f:
            f.write(r.read())


def ffprobe_image_dims(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', path],
        capture_output=True, text=True, check=True
    )
    s = json.loads(r.stdout)['streams'][0]
    return int(s['width']), int(s['height'])


def sanitise_drawtext(s):
    """Escape characters that break FFmpeg drawtext filter."""
    # Order matters: backslash first, then the rest
    s = s.replace('\\', '\\\\')
    s = s.replace("'", "’")   # replace straight apostrophe with curly (safe)
    s = s.replace(':', '\\:')
    s = s.replace('%', '\\%')
    return s


# ── Content generation ────────────────────────────────────────────────────────

def generate_content(post_title, image_url, content_type='news_hook'):
    """Call Gemini to produce structured content for the reel.
    Returns dict with fighter_name, headline, hook_text, cta_text, ig_caption.
    """
    prompt = f"""You are a social-media content writer for PuroMMA (puromma.com),
a Spanish-language MMA site targeting a peninsular Spanish audience (Spain).

Language: ES-ES peninsular Spanish. Use vosotros/os. Use /θ/ distinction in writing
(e.g. "veréis", "conocéis"). Never use LatAm "ustedes" as default second person plural.

Article title: {post_title}
Content type: {content_type}

Return ONLY valid JSON with exactly these keys:
- fighter_name: Main fighter's name in ALL CAPS, 1-3 words (e.g. "MCGREGOR")
- headline: Punchy 3-5 word hook in ALL CAPS (e.g. "SE RETIRA DEL UFC")
- hook_text: A 5-8 word hook line in ALL CAPS, ES-ES peninsular. Provocative,
  opens a curiosity gap or delivers a hot take. No call-to-action here.
  Example: "EL ERROR QUE NADIE OS HA CONTADO"
- body_text: 1-2 short phrases (total 8-14 words) for beats 2-3. The key fact or angle.
  Example: "NO SON LOS NÚMEROS QUE PENSÁIS"
- cta_text: A 8-14 word send/save CTA in sentence case (vosotros imperative).
  Examples: "Mándale esto al que más defiende a este luchador en vuestro grupo."
  or "Mandádselo al grupito de MMA antes de que salga más información."
- ig_caption: Instagram caption in ES-ES, max 180 words before hashtags.
  Format: keyword-rich opening line (15-25 words) + blank line + 2-3 sentences of
  connected prose (no bullet fragments) + blank line + CTA line + blank line +
  5 hashtags on last line: #MMAEspaña #UFC #peleadoresespañoles #MMAEspanol #PuroMMA
- narration: ~30-40 word ES-ES summary of the article for potential TTS use.
"""
    return gemini_json(prompt)


# ── Cover image ───────────────────────────────────────────────────────────────

def make_cover(image_url, tmp_dir):
    """Download + FFmpeg to 1080x1920 brightened JPEG. Returns path."""
    src = os.path.join(tmp_dir, 'cover_src.jpg')
    out = os.path.join(tmp_dir, 'cover.jpg')
    download_image(image_url, src)
    subprocess.run([
        'ffmpeg', '-y', '-i', src,
        '-vf', (
            'scale=1080:1920:force_original_aspect_ratio=increase,'
            'crop=1080:1920,'
            'eq=brightness=0.05:saturation=1.1:contrast=1.05'
        ),
        '-q:v', '3', out
    ], capture_output=True, check=True)
    return out


# ── FFmpeg render ─────────────────────────────────────────────────────────────

def build_beat_zoompan(beat_idx, n_frames):
    """Return zoompan expression string for one beat."""
    if beat_idx == 0:
        # Beat 1: tight center crop, zoom in 1.0→1.35
        z = f"'min(1.0+0.35/{n_frames}*on,1.35)'"
        x = "'iw/2-(iw/zoom/2)'"
        y = "'ih/2-(ih/zoom/2)'"
    elif beat_idx == 1:
        # Beat 2: pan left-to-right with mild zoom 1.1→1.25
        z = f"'min(1.1+0.15/{n_frames}*on,1.25)'"
        x = f"'min(max(iw*0.08*(on/{n_frames}),0),iw-iw/zoom)'"
        y = "'ih/2-(ih/zoom/2)'"
    else:
        # Beat 3: pull-back from 1.35→1.0 (matches beat 1 start — enables seamless loop)
        z = f"'max(1.35-0.35/{n_frames}*on,1.0)'"
        x = "'iw/2-(iw/zoom/2)'"
        y = "'ih/2-(ih/zoom/2)'"
    return z, x, y


def build_drawtext_filters(words_schedule, font_path):
    """
    Build a list of drawtext filter strings for word-by-word kinetic captions.
    words_schedule: list of dicts {word, t_start, t_end, beat, size, color, y_pos}
    """
    filters = []
    font_esc = font_path.replace('\\', '/').replace(':', '\\:')

    for i, w in enumerate(words_schedule):
        word = sanitise_drawtext(w['word'])
        color = w.get('color', 'white')
        size  = w.get('size', 84)
        y_pos = w.get('y_pos', 350)
        t0    = w['t_start']
        t1    = w['t_end']

        # Main word: full opacity
        f = (
            f"drawtext=fontfile='{font_esc}':"
            f"text='{word}':"
            f"fontsize={size}:fontcolor={color}:"
            f"x='(w-tw)/2':y={y_pos}:"
            f"shadowcolor=black@0.85:shadowx=3:shadowy=3:"
            f"enable='between(t,{t0:.3f},{t1:.3f})'"
        )
        filters.append(f)

        # Ghost of previous word (fades out, slightly smaller, at same position)
        if i > 0 and words_schedule[i-1]['beat'] == w['beat']:
            prev = words_schedule[i-1]
            prev_word = sanitise_drawtext(prev['word'])
            prev_size = int(prev.get('size', 84) * 0.92)
            ghost = (
                f"drawtext=fontfile='{font_esc}':"
                f"text='{prev_word}':"
                f"fontsize={prev_size}:fontcolor={prev.get('color','white')}@0.30:"
                f"x='(w-tw)/2':y={prev.get('y_pos', 350)}:"
                f"shadowcolor=black@0.3:shadowx=2:shadowy=2:"
                f"enable='between(t,{t0:.3f},{min(t0+0.25, t1):.3f})'"
            )
            filters.append(ghost)

    return filters


def schedule_words(hook_text, body_text, cta_text):
    """
    Assign word-by-word timing across 3 beats.
    Beat 1 (0-3s): hook_text ALL-CAPS, hook style
    Beat 2+3 (3-9s): body_text words distributed, standard style
    Last beat final 2s: cta_text, red accent color
    Returns list of word timing dicts.
    """
    schedule = []
    WORD_HOLD = 0.35   # seconds per word

    # Beat 1: hook words at top of frame, large, white
    hook_words = hook_text.upper().split()
    t = 0.15  # small lead-in before first word
    for word in hook_words:
        t_end = min(t + WORD_HOLD, 2.9)
        schedule.append({
            'word': word, 'beat': 0,
            't_start': round(t, 3), 't_end': round(t_end, 3),
            'size': 96, 'color': 'white', 'y_pos': 320
        })
        t = t_end
        if t >= 2.9:
            break

    # Beat 2 (3.0-6.0): body words, mid-frame
    body_words = body_text.upper().split() if body_text else []
    t = BEAT_DUR + 0.15
    for word in body_words:
        t_end = min(t + WORD_HOLD, BEAT_DUR * 2 - 0.1)
        schedule.append({
            'word': word, 'beat': 1,
            't_start': round(t, 3), 't_end': round(t_end, 3),
            'size': 80, 'color': 'white', 'y_pos': 800
        })
        t = t_end
        if t >= BEAT_DUR * 2 - 0.1:
            break

    # Beat 3 (6.0-9.0): CTA in red, sentence case, lower centre
    cta_words = cta_text.split() if cta_text else []
    t = BEAT_DUR * 2 + 0.2
    for word in cta_words:
        t_end = min(t + WORD_HOLD, TOTAL_DUR - 0.15)
        schedule.append({
            'word': word, 'beat': 2,
            't_start': round(t, 3), 't_end': round(t_end, 3),
            'size': 68, 'color': '#FF4444', 'y_pos': 950
        })
        t = t_end
        if t >= TOTAL_DUR - 0.15:
            break

    return schedule


def render_video(img_path, content, tmp_dir):
    """
    Build 3-beat hard-cut reel with kinetic captions.
    Returns path to rendered MP4.
    """
    hook_text = content.get('hook_text', content.get('headline', 'PURO MMA'))
    body_text = content.get('body_text', content.get('headline', ''))
    cta_text  = content.get('cta_text', 'Mándale esto a un amigo aficionado al MMA.')
    fighter   = content.get('fighter_name', '')

    out_path = os.path.join(tmp_dir, 'reel.mp4')
    n_frames = int(BEAT_DUR * FPS)  # frames per beat

    # Build 3-beat filter_complex
    beat_segments = []
    for bi in range(3):
        z, x, y = build_beat_zoompan(bi, n_frames)
        seg = (
            f"[0:v] scale={WORK_W}:{WORK_H}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={WORK_W}:{WORK_H},"
            f"zoompan=z={z}:x={x}:y={y}:d={n_frames}:s=1080x1920:fps={FPS},"
            f"unsharp=luma_msize_x=3:luma_msize_y=3:luma_amount=0.6"
            f" [b{bi}]"
        )
        beat_segments.append(seg)

    # Concat beats
    fc_parts = beat_segments
    fc_parts.append(f"[b0][b1][b2] concat=n=3:v=1:a=0 [pre_text]")

    # Build word-by-word caption filters
    words_schedule = schedule_words(hook_text, body_text, cta_text)
    caption_filters = build_drawtext_filters(words_schedule, FONT_PATH)

    # Fighter name watermark (bottom-centre, subtle)
    font_esc = FONT_PATH.replace('\\', '/').replace(':', '\\:')
    if fighter:
        fighter_safe = sanitise_drawtext(fighter)
        caption_filters.append(
            f"drawtext=fontfile='{font_esc}':"
            f"text='{fighter_safe}':"
            f"fontsize=38:fontcolor=white@0.55:"
            f"x='(w-tw)/2':y=1780:"
            f"shadowcolor=black@0.5:shadowx=2:shadowy=2"
        )

    # Red accent rule — horizontal line below hook beat
    caption_filters.append(
        f"drawbox=x=60:y=430:w=960:h=4:color=#FF4444@0.85:t=fill:"
        f"enable='lte(t,{BEAT_DUR:.1f})'"
    )

    # Dark overlay bars for readability (hook beat + CTA beat)
    readability = [
        # Hook beat background bar
        f"drawbox=x=0:y=270:w=1080:h=200:color=black@0.55:t=fill:"
        f"enable='lte(t,{BEAT_DUR:.1f})'",
        # Body beat background bar
        f"drawbox=x=0:y=755:w=1080:h=140:color=black@0.50:t=fill:"
        f"enable='between(t,{BEAT_DUR:.1f},{BEAT_DUR*2:.1f})'",
        # CTA beat background bar
        f"drawbox=x=0:y=905:w=1080:h=130:color=black@0.55:t=fill:"
        f"enable='gte(t,{BEAT_DUR*2:.1f})'",
    ]

    all_text_filters = ','.join(readability + caption_filters)
    fc_parts.append(f"[pre_text] {all_text_filters} [v_text]")

    # Logo overlay (bottom-right corner, constant)
    has_logo = os.path.exists(LOGO_PATH)
    logo_w = int(1080 * 0.13)   # ~140px (13% of frame width)
    if has_logo:
        fc_parts.append(f"[2:v] scale={logo_w}:-1 [logo]")
        logo_x = 1080 - logo_w - 20
        logo_y = 1920 - logo_w - 20  # square logo ~same dimensions
        fc_parts.append(
            f"[v_text][logo] overlay={logo_x}:{logo_y}:format=auto,"
            f"format=yuv420p,"
            f"setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709 [v]"
        )
    else:
        fc_parts.append(
            f"[v_text] format=yuv420p,"
            f"setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709 [v]"
        )

    # Audio: music bed trimmed to total duration, volume 0.08
    music_dur_check = audio_duration(MUSIC_PATH)
    fade_out_st = TOTAL_DUR - 1.5
    fc_parts.append(
        f"[1:a] atrim=0:{TOTAL_DUR},asetpts=PTS-STARTPTS,"
        f"volume=0.08,"
        f"afade=t=in:st=0:d=0.8,"
        f"afade=t=out:st={fade_out_st:.2f}:d=1.4 [audio]"
    )

    filter_complex = '; '.join(fc_parts)

    # Build inputs
    inputs = [
        '-loop', '1', '-framerate', str(FPS), '-t', str(TOTAL_DUR + 0.5), '-i', img_path,
        '-i', MUSIC_PATH,
    ]
    if has_logo:
        inputs += ['-i', LOGO_PATH]

    cmd = (
        ['ffmpeg', '-y'] + inputs +
        [
            '-filter_complex', filter_complex,
            '-map', '[v]', '-map', '[audio]',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-profile:v', 'high',
            '-movflags', '+faststart',
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '2',
            '-t', str(TOTAL_DUR), '-r', str(FPS),
            out_path
        ]
    )

    print(f'  FFmpeg render ({TOTAL_DUR}s, 3 beats)...')
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr_tail = (result.stderr or '')[-4000:]
        print(f'  FFmpeg stderr:\n{stderr_tail}')
        raise RuntimeError(f'FFmpeg failed (rc={result.returncode})')
    print(f'  Render complete: {out_path}')
    return out_path


# ── R2 upload ─────────────────────────────────────────────────────────────────

def upload_to_r2(local_path, object_key, content_type):
    """Upload file to R2 via aws-cli (pre-installed on ubuntu-latest)."""
    # aws-cli reads AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from environment
    # (set in the GH Actions workflow as R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)
    cmd = [
        'aws', 's3', 'cp', local_path,
        f's3://{R2_BUCKET}/{object_key}',
        '--endpoint-url', R2_ENDPOINT,
        '--content-type', content_type,
        '--no-progress',
    ]
    print(f'  Uploading {object_key} → R2...')
    result = subprocess.run(cmd, capture_output=True, text=True, env=dict(
        os.environ,
        AWS_ACCESS_KEY_ID=os.environ['R2_ACCESS_KEY_ID'],
        AWS_SECRET_ACCESS_KEY=os.environ['R2_SECRET_ACCESS_KEY'],
        AWS_DEFAULT_REGION='auto',
    ))
    if result.returncode != 0:
        raise RuntimeError(f'R2 upload failed: {result.stderr[:500]}')
    public_url = f'{R2_PUBLIC_URL.rstrip("/")}/{object_key}'
    print(f'  Uploaded: {public_url}')
    return public_url


# ── Callback ──────────────────────────────────────────────────────────────────

def post_callback(callback_url, payload, token=None):
    """POST result to n8n callback URL. Silently skips if no callback_url."""
    if not callback_url:
        print('  No callback_url — skipping callback.')
        return
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    data = json.dumps(payload).encode()
    req = urllib.request.Request(callback_url, data=data, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f'  Callback → {r.status}')
    except Exception as e:
        print(f'  Callback failed (non-fatal): {e}')


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main():
    raw_payload = os.environ.get('JOB_PAYLOAD', '{}')
    job = json.loads(raw_payload)

    job_id       = job.get('job_id', f"puromma_{int(time.time())}")
    post_title   = job.get('post_title', 'PuroMMA Article')
    image_url    = job.get('image_url', '')
    wp_post_id   = job.get('wp_post_id', '0')
    content_type = job.get('content_type', 'news_hook')
    callback_url = job.get('callback_url', '')
    n8n_token    = os.environ.get('N8N_CALLBACK_TOKEN', '')

    print(f'=== PuroMMA Render Pipeline ===')
    print(f'  job_id: {job_id}')
    print(f'  post_title: {post_title[:80]}')
    print(f'  content_type: {content_type}')
    print(f'  font: {FONT_PATH}')

    if not image_url:
        raise ValueError('image_url is required in JOB_PAYLOAD')

    ts = int(time.time())
    filename      = f'puromma_{wp_post_id}_{ts}.mp4'
    cover_filename = f'puromma_{wp_post_id}_{ts}_cover.jpg'

    tmp_dir = tempfile.mkdtemp(prefix='reel_')

    try:
        # 1. Content generation
        print('  Generating content (Gemini)...')
        content = generate_content(post_title, image_url, content_type)
        print(f"  fighter: {content.get('fighter_name')} | hook: {content.get('hook_text', '')[:60]}")

        # Allow payload overrides (Pablo or n8n can pass pre-written copy)
        if job.get('hook_text'):
            content['hook_text'] = job['hook_text']
        if job.get('cta_text'):
            content['cta_text'] = job['cta_text']

        # 2. Download fighter image
        img_path = os.path.join(tmp_dir, 'fighter.jpg')
        print(f'  Downloading image: {image_url[:80]}...')
        download_image(image_url, img_path)

        # 3. Render video
        mp4_path = render_video(img_path, content, tmp_dir)

        # 4. Cover image
        print('  Generating cover...')
        cover_path = make_cover(image_url, tmp_dir)

        # 5. Upload to R2
        video_url = upload_to_r2(mp4_path, filename, 'video/mp4')
        cover_url = upload_to_r2(cover_path, cover_filename, 'image/jpeg')

        # 6. Output to Actions summary / step output
        ig_caption = content.get('ig_caption', post_title)
        duration   = TOTAL_DUR

        summary_lines = [
            '## PuroMMA Render Complete',
            f'- **Job ID:** {job_id}',
            f'- **Fighter:** {content.get("fighter_name", "?")}',
            f'- **Hook:** {content.get("hook_text", "?")}',
            f'- **Duration:** {duration}s',
            f'- **Video URL:** {video_url}',
            f'- **Cover URL:** {cover_url}',
            f'- **IG Caption (first 200 chars):** {ig_caption[:200]}',
        ]
        print('\n'.join(summary_lines))

        # Write to GITHUB_STEP_SUMMARY if available
        summary_file = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary_file:
            with open(summary_file, 'a') as f:
                f.write('\n'.join(summary_lines) + '\n')

        # Write step outputs if GITHUB_OUTPUT available
        # ig_caption may contain newlines — use multiline heredoc syntax
        output_file = os.environ.get('GITHUB_OUTPUT')
        if output_file:
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(f'video_url={video_url}\n')
                f.write(f'cover_url={cover_url}\n')
                # Multiline value syntax for GITHUB_OUTPUT
                caption_safe = ig_caption[:500].replace('\r', '')
                delim = 'EOF_CAPTION'
                f.write(f'ig_caption<<{delim}\n{caption_safe}\n{delim}\n')

        # 7. Callback to n8n (if provided)
        result_payload = {
            'job_id':     job_id,
            'status':     'success',
            'video_url':  video_url,
            'cover_url':  cover_url,
            'ig_caption': ig_caption,
            'duration':   duration,
            'filename':   filename,
        }
        post_callback(callback_url, result_payload, n8n_token)

        print('=== Pipeline complete ===')

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f'PIPELINE ERROR: {tb}')
        error_payload = {
            'job_id': job_id,
            'status': 'error',
            'error':  str(e),
        }
        post_callback(callback_url, error_payload, n8n_token)
        sys.exit(1)

    finally:
        import shutil
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass


if __name__ == '__main__':
    main()
