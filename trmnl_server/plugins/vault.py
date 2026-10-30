"""Vault plugin: the week as the Life vault sees it, from one JSON file.

A host job decrypts the vault, runs its screen.py and leaves screen.json where
this container can read it (VAULT_SCREEN_JSON, read-only). This plugin draws
that file and nothing else: no git, no password, no vault inside the container.
The JSON's keys are the contract documented in homelab-docs VAULT-SCREEN.md.

Drawn for a Kindle Paperwhite 11 (1236x1648 portrait, 16 greys); the layout is
the mock settled there on 2026-10-29.
"""
import asyncio
import datetime
import json
import logging
import math
import os
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from ..utils import asset_path
from .base import PluginBase, PluginOutput

logger = logging.getLogger(__name__)

DEFAULT_JSON = os.getenv('VAULT_SCREEN_JSON', '/app/var/vault/screen.json')
# Older than this and the host job has stopped; the screen says so instead of
# showing a week that is no longer current.
STALE_AFTER = datetime.timedelta(hours=2)

W, H = 1236, 1648
M = 56                      # outer margin
GAP = 20                    # between task cards
SEC = 40                    # between sections
PAD = 22                    # inside a task card
COLS = 2
CARD_H = 136                # one task card, two lines of text
BOX = 34                    # checkbox side
HAB_ROW = 44                # one habit line in a group
WARN_H = 260                # reserved for warnings, always
FOOTER_H = 110
# Four grey levels, matching save_assets' default quantisation.
INK, MID, LIGHT, PAPER = 0, 85, 170, 255

REGULAR = asset_path('fonts/ttf/static/SpaceGrotesk-Regular.ttf').as_posix()
BOLD = asset_path('fonts/ttf/static/SpaceGrotesk-Bold.ttf').as_posix()

GROUPS = [('Today', ('today', 'soon', 'done')), ('Late', ('missed',)), ('Overdue', ('over',))]


class VaultPlugin(PluginBase):
    """Draw the vault's screen.json: the three, due tasks, habits, warnings."""

    BASENAME = 'vault'
    OUTPUT_SUBDIR = 'vault'
    REGISTRY_ORDER = 5
    REFRESH_INTERVAL = 900
    DISPLAY_NAME = 'Vault'

    def __init__(self):
        super().__init__()
        self.json_path = DEFAULT_JSON

    def get_content_ttl(self) -> int:
        return 900          # the host job rewrites the file every 15 minutes

    async def run(self, **kwargs) -> Optional[PluginOutput]:
        output_dir = kwargs.get('output_dir', 'web')
        os.makedirs(output_dir, exist_ok=True)
        try:
            data = await asyncio.to_thread(self._load)
        except (OSError, ValueError) as exc:
            logger.warning('Vault screen.json unreadable at %s: %s', self.json_path, exc)
            return None
        image = await asyncio.to_thread(self.render, data)
        return await asyncio.to_thread(self.save_assets, image, output_dir, self.BASENAME)

    def _load(self) -> dict:
        with open(self.json_path, encoding='utf-8') as fh:
            return json.load(fh)

    # --- drawing -----------------------------------------------------------

    def render(self, d: dict) -> Image.Image:
        f_week = self.load_font(120, (BOLD,))
        f_h = self.load_font(32, (BOLD,))
        f_body = self.load_font(34, (REGULAR,))
        f_bold = self.load_font(34, (BOLD,))
        f_small = self.load_font(27, (REGULAR,))
        f_warn = self.load_font(36, (BOLD,))
        # Space Grotesk runs wide: the three habit columns take a size down.
        f_hab = self.load_font(30, (REGULAR,))
        f_habb = self.load_font(30, (BOLD,))

        img = Image.new('L', (W, H), PAPER)
        draw = ImageDraw.Draw(img)
        x0, x1 = M, W - M

        def heading(y, text, fill=MID):
            draw.text((x0, y), text.upper(), font=f_h, fill=fill)
            return y + 50

        # header
        draw.text((M, M - 36), d['week'][-3:], font=f_week, fill=INK)
        draw.text((x1, M + 10), d['week'][:4], font=f_small, fill=MID, anchor='ra')
        built = _built(d.get('built', ''))
        draw.text((x1, M + 52), built, font=f_small, fill=INK if 'stale' in built else MID, anchor='ra')
        y = M + 130

        warn_top = H - M - FOOTER_H - SEC - WARN_H
        floor = warn_top - SEC

        hs = [h for h in d['habits'] if h['state'] != 'quiet']
        tallest = max((sum(1 for h in hs if h['state'] in st) for _, st in GROUPS), default=0)
        habit_h = 50 + 50 + HAB_ROW * tallest + SEC if hs else 0

        # the three
        y = heading(y, 'The three')
        due_by_label = {it['label']: it for it in d['due']}
        three = []
        for t in d['three']:
            it = due_by_label.get(t['label'])
            three.append((t['label'], _when(it['days'], it['date']) if it else None, True, t.get('week', False)))
        if three:
            y, _ = self._cards(draw, x0, y, x1, three, floor - habit_h, f_body, f_small)
        else:
            draw.text((x0, y), 'no star this week', font=f_body, fill=MID)
            y += 50
        y += SEC

        # due, without the starred ones (already above)
        due = [(it['label'], _when(it['days'], it['date']), False, False) for it in d['due'] if not it.get('starred')]
        late = ' · %d overdue' % d['overdue'] if d['overdue'] else ''
        y = heading(y, 'Due in 7 days' + late)
        if due:
            y, n = self._cards(draw, x0, y, x1, due, floor - habit_h - 36, f_body, f_small)
            if n < len(due):
                draw.text((x1, y + 6), '+ %d more' % (len(due) - n), font=f_small, fill=MID, anchor='ra')
                y += 36
        else:
            draw.text((x0, y), 'nothing else dated', font=f_body, fill=MID)
            y += 50
        y += SEC

        if hs:
            y = heading(y, 'Habits')
            self._habit_groups(draw, x0, y, x1, hs, f_h, f_hab, f_habb)

        # warnings: reserved area, plain rows, no box
        warns = d['warnings'][:3]
        draw.line((x0, warn_top, x1, warn_top), fill=LIGHT, width=3)
        ty = heading(warn_top + 20, 'Needs a look', INK if warns else LIGHT)
        if not warns:
            draw.text((W // 2, warn_top + WARN_H // 2 + 20), 'nothing to flag', font=f_body, fill=LIGHT, anchor='mm')
        for w in warns:
            draw.rounded_rectangle((x0, ty + 6, x0 + 44, ty + 50), radius=8, fill=INK)
            draw.text((x0 + 22, ty + 28), '!', font=f_warn, fill=PAPER, anchor='mm')
            draw.text((x0 + 68, ty), _cut(draw, w['title'], f_warn, 560), font=f_warn, fill=INK)
            draw.text((x1, ty + 10), _cut(draw, w['detail'], f_small, x1 - x0 - 660), font=f_small, fill=MID, anchor='ra')
            ty += 66

        # footer: next external date, open questions
        fy = H - M - FOOTER_H
        draw.line((x0, fy, x1, fy), fill=LIGHT, width=3)
        fy += 20
        nx = d['next_external']
        q = d['questions']
        qt = ('%d open question' % q + ('s' if q != 1 else '')) if q else 'no open questions'
        if nx:
            dt = datetime.date.fromisoformat(nx['date'])
            draw.text((x0, fy), _cut(draw, 'Next: ' + nx['label'], f_body, x1 - x0 - 360), font=f_body, fill=INK)
            draw.text((x0, fy + 54), '%s · in %d d' % (dt.strftime('%a %d %b'), nx['days']), font=f_small, fill=MID)
        else:
            draw.text((x0, fy), 'Nothing dated in 90 days', font=f_body, fill=MID)
        draw.text((x1, fy + 54), qt, font=f_small, fill=MID, anchor='ra')
        return img

    def _cards(self, draw, x0, y, x1, items, floor, f_body, f_small) -> Tuple[int, int]:
        """Lay (text, tag, starred, week) items out in COLS columns. Returns y below them and how many fit."""
        cw = (x1 - x0 - GAP * (COLS - 1)) // COLS
        n = 0
        for i, (text, tag, starred, week) in enumerate(items):
            row, col = divmod(i, COLS)
            cy = y + row * (CARD_H + GAP)
            if cy + CARD_H > floor:
                break
            cx = x0 + col * (cw + GAP)
            self._task_card(draw, cx, cy, cx + cw, text, tag, starred, week, f_body, f_small)
            n += 1
        rows = math.ceil(n / COLS)
        return (y + rows * (CARD_H + GAP) - GAP if n else y), n

    @staticmethod
    def _task_card(draw, x0, y, x1, text, tag, starred, week, f_body, f_small) -> None:
        """One task, one card: checkbox, two lines of words, the tag on the second line's right.
        Starred: heavy border and a star. A week task shows an hourglass where the date would be."""
        draw.rounded_rectangle((x0, y, x1, y + CARD_H), radius=18, fill=PAPER,
                               outline=INK if starred else LIGHT, width=6 if starred else 3)
        tx = x0 + PAD
        draw.rounded_rectangle((tx, y + PAD + 4, tx + BOX, y + PAD + 4 + BOX), radius=6, outline=INK, width=4)
        if starred:
            _star(draw, x1 - PAD - 16, y + PAD + 18, 16)
        tx += BOX + 16
        tw = (draw.textlength(tag, font=f_small) if tag else 40 if week else 0) + 16
        width = x1 - PAD - tx
        ty = y + PAD
        for line in _wrap(draw, text, f_body, [width - (40 if starred else 0), width - tw]):
            draw.text((tx, ty), line, font=f_body, fill=INK)
            ty += 44
        ly = y + PAD + 44 + 22          # the second line's middle
        if tag:
            draw.text((x1 - PAD, ly), tag, font=f_small, fill=MID, anchor='rm')
        elif week:
            _hourglass(draw, x1 - PAD - 10, ly)

    @staticmethod
    def _habit_groups(draw, x0, y, x1, hs, f_h, f_body, f_bold) -> None:
        """Three columns: today in bold, late with a hatched marker, overdue faint."""
        cw = (x1 - x0 - GAP * 2) // 3
        for i, (name, states) in enumerate(GROUPS):
            cx = x0 + i * (cw + GAP)
            rows = [h for h in hs if h['state'] in states]
            draw.text((cx, y), name.upper(), font=f_h, fill=INK if rows else LIGHT)
            ry = y + 50
            for h in rows:
                st, text = h['state'], h['name']
                if st == 'soon':
                    text += ' · %d d left' % (h['maxGap'] - h['gap'])
                elif st == 'over':
                    text += ' · %d d' % (h['gap'] - h['maxGap'])
                if st == 'missed':
                    _hatch(draw, cx, ry + 6, cx + 26, ry + 32)
                    draw.rectangle((cx, ry + 6, cx + 26, ry + 32), outline=MID, width=2)
                    draw.text((cx + 40, ry), _cut(draw, text, f_body, cw - 40), font=f_body, fill=INK)
                elif st == 'over':
                    draw.text((cx, ry), _cut(draw, text, f_body, cw), font=f_body, fill=LIGHT)
                else:
                    mark = '✓ ' if st == 'done' else ''
                    draw.text((cx, ry), _cut(draw, mark + text, f_bold, cw), font=f_bold, fill=INK)
                ry += HAB_ROW


# --- helpers ---------------------------------------------------------------

def _built(built: str) -> str:
    """'built HH:MM', or how stale the file is when the host job has gone quiet."""
    try:
        when = datetime.datetime.strptime(built, '%Y-%m-%d %H:%M')
    except ValueError:
        return 'built ?'
    age = datetime.datetime.now() - when
    if age > STALE_AFTER:
        return 'stale · %d h' % (age.total_seconds() // 3600)
    return 'built ' + built[-5:]


def _cut(draw, text, fnt, width) -> str:
    """One line, cut early with an ellipsis."""
    if draw.textlength(text, font=fnt) <= width:
        return text
    while text and draw.textlength(text + '…', font=fnt) > width:
        text = text[:-1].rstrip()
    return text + '…'


def _wrap(draw, text, fnt, widths: Sequence[int]) -> List[str]:
    """One line per width, the last one cut with an ellipsis."""
    out, words = [], text.split()
    for width in widths:
        if not words:
            break
        line = words.pop(0)
        while words and draw.textlength(line + ' ' + words[0], font=fnt) <= width:
            line += ' ' + words.pop(0)
        out.append(line)
    if words:
        out[-1] = _cut(draw, out[-1] + ' ' + ' '.join(words), fnt, widths[len(out) - 1])
    return out


def _when(days: int, date: str) -> str:
    if days < 0:
        return '%d d late' % -days
    if days == 0:
        return 'today'
    if days == 1:
        return 'tomorrow'
    return '%s · %d d' % (datetime.date.fromisoformat(date).strftime('%a'), days)


def _star(draw, cx, cy, r, fill=INK) -> None:
    pts = []
    for i in range(10):
        a = math.radians(-90 + i * 36)
        rr = r if i % 2 == 0 else r * 0.42
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    draw.polygon(pts, fill=fill)


def _hourglass(draw, cx, cy, h=26, w=20) -> None:
    """A week task's mark: two triangles tip to tip, sand in the lower one."""
    x0, x1, y0, y1 = cx - w // 2, cx + w // 2, cy - h // 2, cy + h // 2
    draw.line((x0, y0, x1, y0), fill=MID, width=3)
    draw.line((x0, y1, x1, y1), fill=MID, width=3)
    draw.polygon([(x0, y0), (x1, y0), (cx, cy)], outline=MID, width=2)
    draw.polygon([(x0, y1), (x1, y1), (cx, cy)], fill=MID)


def _hatch(draw, x0, y0, x1, y1, step=10) -> None:
    """Diagonal hatching clipped to a rectangle."""
    for s in range(-(y1 - y0), x1 - x0, step):
        ax, ay = x0 + s, y0
        bx, by = x0 + s + (y1 - y0), y1
        if ax < x0:
            ay, ax = y0 + (x0 - ax), x0
        if bx > x1:
            by, bx = y1 - (bx - x1), x1
        draw.line((ax, ay, bx, by), fill=MID, width=2)
