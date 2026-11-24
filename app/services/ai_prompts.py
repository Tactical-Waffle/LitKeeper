"""The prompts LitKeeper sends to OpenRouter for AI covers, editable in Settings → AI Covers.

Each prompt has a built-in default; a saved prompt replaces it, and saving an empty box goes back to the
default. The picture prompts are templates: `{description}` is replaced by the scene the text model wrote,
`{title}` and `{author}` by the story's title and author, and `{title_space}` by how much of the top of
the cover the title will take up.
"""
from __future__ import annotations
import re

KEY = 'ai_cover_prompts'
MAX_LENGTH = 6000

COVER_BRIEF = """You are the art director for the covers of a library of adult romance and erotic fiction. Read the story details below (title, category, tags, the story's own description and passages from the story) and write one detailed prompt for an image model.

Choose the moment:
- Pick the most emotionally charged moment of intimacy between the central characters that can be shown clothed: an embrace, a hand at the waist or on a cheek, foreheads almost touching, a whisper at the ear, the instant before a kiss, a lingering touch, or a charged look across a small space. Prefer this over a handshake, a meeting at a distance, or objects.
- Use a moment, place and people that actually appear in the passages. Show only the people present in that moment, usually two. Do not invent contact or actions the story contradicts.

Describe the people precisely:
- For each person: approximate age, build, face, hair (colour, length, style, any grey exactly as described), skin tone if stated, clothing (garment, colour, fit), posture, hands, expression and where they are looking.
- Keep stated details exact. If an adult's age is unspecified, make them about 30. Do not present invented traits as facts from the story. Everyone shown is an adult; never depict anyone under 18.

Keep it suggestive, not explicit:
- Fully clothed or in setting-appropriate clothing; no nudity, exposed breasts or genitals, sex acts, or explicit wording. Sensuality comes from closeness, touch, gaze, body language, light and framing, in the style of a contemporary romance cover.
- No violence or gore.

Describe the setting and image:
- The specific location from the story with two or three telling details, kept faithful (an executive office is orderly unless the story says otherwise).
- Cinematic editorial photography: camera distance and angle, lens feel, shallow depth of field, a plausible light source, colour palette, natural skin and fabric texture, subtle film grain.
- Vertical 3:4. The characters fill the middle of the frame; keep the top fifth darker and simpler (sky, wall, shadow). Do not describe any title, lettering or other text; the cover's lettering is handled separately. No signs, logos or screens showing text.

Reply with the image prompt only: 150 to 250 words of plain prose, with no title, list, quotation marks or explanation."""

COVER_IMAGE = (
    '{description}\n\n'
    'Vertical 3:4 fiction cover artwork. Grounded editorial photography, believable details, natural textures, subtle grain. '
    'Keep the top {title_space} of the frame simple, uncluttered and darker (sky, wall, ceiling or soft shadow), and the '
    'bottom tenth plain as well, with no faces, hands or important details there. No visible text, lettering, numbers, '
    'logos, or watermark.')

COVER_IMAGE_LETTERED = (
    'Tall vertical 3:4 portrait book cover, taller than it is wide. Front cover of a novel titled "{title}" '
    'with the author name "{author}" at the bottom, a cinematic editorial photograph of this scene: {description}\n\n'
    'Full-height vertical composition where the characters fill the middle of the tall frame while the top fifth stays dark '
    'and simple. Lettering: the title "{title}" exactly as written, on no more than two lines in that upper '
    'space, set in a tall serif with generous letter spacing; the author name "{author}" exactly as written, with the same '
    'capitalisation, at the bottom in the same font and the same colour as the title but smaller. All lettering in one font '
    'and one colour. Natural skin, believable fabric texture and subtle film grain. Strictly 3:4 vertical aspect ratio, all '
    'text legible and spelled exactly, no other text: no taglines, no screen text, no signs, no logos, no watermark.')

COVER_SAFER = """An image service's safety filter refused the cover description below. Rewrite it so a general-audience image service will accept it, keeping the same place, people and emotional moment as closely as you can.

- Everyday clothing that fully covers everyone: replace towels, bathrobes, underwear, wet or bare skin with ordinary clothes.
- No sexual, bathing, undressing or bedroom wording. Tension comes only from closeness, gaze, posture and light.
- Everyone is clearly an adult: keep stated ages of 25 or over; describe anyone stated as younger simply as a young adult man or woman in their mid-twenties.
- Keep the camera, lighting, palette and composition notes, including any note about keeping the top of the frame simple.

Reply with the rewritten description only, about the same length."""

SUMMARY = """You write the short summaries shown in a reading library. Read the story details below and summarize the story.

Rules:
- At most two sentences and 45 words in all.
- Say who the story is about and what happens, in the present tense. Give the setup and the central turn; do not reveal the ending.
- Plain, non-explicit wording: describe sexual content, if any, in general terms only.
- Do not begin with "This story", "In this story" or the title, and do not mention tags, parts, chapters or Literotica.

Reply with the summary only, with no quotation marks or introduction."""

REWRITE = """You are a careful copy editor for adult fiction. Proofread and copy-edit the story part below.

- Fix spelling, grammar, punctuation, capitalisation and paragraphing, and smooth clumsy sentences.
- Keep the author's story, voice, tense, point of view, dialogue, explicitness and every event exactly as they are. Do not add, remove, soften or summarise anything.
- Keep the length within about 10% of the original.
- Where a character is called only by an initial (such as "B"), give them a fitting first name starting with that letter and use it consistently. If the text is preceded by "Character names already used", keep using those names.
- Do not use em or en dashes; use commas, full stops or parentheses instead.
- Separate paragraphs with a blank line. Use plain text, with no headings, notes or Markdown.

Reply with the edited text only."""

PROMPTS = {
    'cover_brief': {'label': 'Scene description (text model)', 'default': COVER_BRIEF,
                    'help': 'Instructions for the text model. It also receives the title, category, tags, the story\'s '
                            'description and story text (how much is set above).'},
    'cover_image': {'label': 'Picture, LitKeeper adds the lettering (image model)', 'default': COVER_IMAGE,
                    'help': '{description} is the written scene; {title_space} is how much of the top the title will cover.'},
    'cover_image_lettered': {'label': 'Picture with title and author (image model)', 'default': COVER_IMAGE_LETTERED,
                             'help': 'Used when the image model letters the cover. {description} is the written scene, '
                                     '{title} the title and {author} the author.'},
    'cover_safer': {'label': 'Toned-down scene after a refusal (text model)', 'default': COVER_SAFER,
                    'help': 'Used when every image model refused a cover. It receives only the refused description.'},
    'summary': {'label': 'AI summary (text model)', 'default': SUMMARY,
                'help': 'Instructions for Make Summary. It receives the same title, category, tags, description and story '
                        'text as the cover\'s scene description (how much is set above).'},
    'rewrite': {'label': 'AI Rewrite (only when the rewrite model is not a preset)', 'default': REWRITE,
                'help': 'Sent ahead of each part when Settings → AI Rewrite names a model. An OpenRouter preset '
                        '(@preset/…) uses its own prompt instead, and this one is not sent.'},
}


def _saved() -> dict:
    from app.models import AppConfig
    config = AppConfig.query.filter_by(key=KEY).first()
    value = config.get_value() if config else None
    return value if isinstance(value, dict) else {}


def get(name: str) -> str:
    return (_saved().get(name) or '').strip() or PROMPTS[name]['default']


def overview() -> list[dict]:
    """Each prompt for the Settings page: its text, default, and whether it was changed."""
    saved = _saved()
    return [{'name': name, 'label': info['label'], 'help': info['help'], 'default': info['default'],
             'text': (saved.get(name) or '').strip() or info['default'],
             'changed': bool((saved.get(name) or '').strip()) and saved[name].strip() != info['default']}
            for name, info in PROMPTS.items()]


def save(values: dict) -> None:
    """Save prompts from the form; an empty box or an unchanged default restores the default."""
    from app.models import AppConfig, db
    saved = _saved()
    for name, info in PROMPTS.items():
        if name not in values:
            continue
        text = str(values.get(name) or '').replace('\r\n', '\n').strip()
        if len(text) > MAX_LENGTH:
            raise ValueError(f'{info["label"]}: keep the prompt under {MAX_LENGTH:,} characters.')
        if name in ('cover_image', 'cover_image_lettered') and text and '{description}' not in text:
            raise ValueError('The picture prompts must contain {description}, where the written scene goes.')
        saved[name] = '' if text == info['default'] else text
    config = AppConfig.query.filter_by(key=KEY).first()
    if not config:
        config = AppConfig(key=KEY, value_type='json', description='Prompts for AI covers, summaries and rewrites')
        db.session.add(config)
    config.set_value({name: value for name, value in saved.items() if value})
    db.session.commit()


def tidy_reply(message: str) -> str:
    """The model's reply as one paragraph, without a leading label or surrounding quotes."""
    message = re.sub(r'(?im)^[\s*#]*image prompt[*\s]*:[ \t*]*', '', message or '')
    return re.sub(r'\s+', ' ', message).strip().strip('"“”*').strip()
