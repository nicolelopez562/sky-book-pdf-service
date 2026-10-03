"""
THE SKY THAT MADE YOU — Book PDF Assembly Service
====================================================================
Receives a JSON payload (the output of the Zapier Code step that
builds the 26-page list + overlay text), downloads each page image,
draws the required text overlays on top of the 5 dynamic pages, and
assembles everything into one finished PDF.

Deploy this as a small web service (Render, Railway, Fly.io, or
similar free/cheap tier all work fine for this). Zapier calls it
with a single "Webhooks by Zapier" POST step, same pattern already
used for the Astrologer API and Lulu calls earlier in this project.

ENDPOINT: POST /generate-pdf
BODY: the full JSON object the Zapier Code step already outputs
      (pages array, childName, dobDisplay, tobDisplay, city,
      sunSign, moonSign, risingSign, sunSuperpower, moonSuperpower,
      risingSuperpower)
RETURNS: { "pdf_url": "..." } — a URL where the finished PDF can be
         downloaded (this starter version saves locally and returns
         a placeholder; see the "STORAGE" note near the bottom for
         what to change for production).
====================================================================
"""

from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
import requests
from io import BytesIO
import os
import uuid

app = Flask(__name__)

# ----------------------------------------------------------------
# PAGE DIMENSIONS — the source PNGs are 2000x2000px squares
# (confirmed earlier from the Canva export). We work at that same
# resolution throughout, then let the PDF step handle final sizing.
# ----------------------------------------------------------------
PAGE_SIZE = 2000

# ----------------------------------------------------------------
# FONT PATHS — Baloo 2 (headline) and Nunito (body) are the book's
# established fonts throughout this whole project. Upload matching
# .ttf files alongside this script and point these paths at them.
# Google Fonts has free downloadable versions of both.
# ----------------------------------------------------------------
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FONT_HEADLINE = os.path.join(FONT_DIR, "Baloo2-Bold.ttf")
FONT_BODY = os.path.join(FONT_DIR, "Nunito-Bold.ttf")

PURPLE = (108, 74, 182)   # #6C4AB6, matches the book's established accent purple


def load_font(path, size):
    """Loads a font at the given size, falling back to a default
    font if the real one isn't available yet (so testing doesn't
    break before fonts are uploaded)."""
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def fetch_image(url):
    """Downloads a page image from its Shopify URL and returns it
    as a PIL Image, resized to our working canvas size if needed."""
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    img = Image.open(BytesIO(response.content)).convert("RGB")
    if img.size != (PAGE_SIZE, PAGE_SIZE):
        img = img.resize((PAGE_SIZE, PAGE_SIZE), Image.LANCZOS)
    return img


def draw_centered_text(draw, text, center_x, center_y, font, fill=PURPLE):
    """Draws text centered horizontally and vertically around a
    given point — the simplest reliable way to place overlay text
    without needing exact box coordinates from Canva."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text((center_x - text_w / 2, center_y - text_h / 2), text, font=font, fill=fill)


# ----------------------------------------------------------------
# OVERLAY POSITIONS — these are ESTIMATES based on the book's
# established layout conventions (large centered headline text,
# generous margins). Treat every (x, y) and font size here as a
# starting point: generate one real test PDF, compare it against
# the actual Canva pages, and adjust these numbers until they
# match exactly. This is the normal way this kind of positioning
# gets dialed in — nothing here is guesswork meant to be final.
# ----------------------------------------------------------------

def overlay_welcome(img, data):
    """Page 5 — 'Hi [Name]' page. Name sits in its own isolated
    box per the redesign done earlier this session."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_HEADLINE, 160)
    # Estimated center point for the name box, based on the real
    # coordinates confirmed earlier this session (top:84, left:282,
    # width:257, at 2000px canvas scale).
    draw_centered_text(draw, data["childName"], center_x=820, center_y=330, font=font)
    return img


def overlay_birthday(img, data):
    """Page 6 — birthday reveal, with dedicated pill-shaped
    backgrounds behind the time and city text (per the redesign)."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 56)
    # Time pill — confirmed earlier at top:202, left:387, width:136
    draw_centered_text(draw, data["tobDisplay"], center_x=1360, center_y=640, font=font)
    # City pill — confirmed earlier at top:241, left:283, width:288
    draw_centered_text(draw, data["city"], center_x=1360, center_y=760, font=font)
    return img


def overlay_magic_of_you(img, data):
    """Page 16 — 'a [Sun] heart, a [Moon] soul, and a [Rising]
    spark'. Position is an estimate — confirm against the real
    page once a test PDF is generated."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 70)
    line = f'a {data["sunSign"]} heart, a {data["moonSign"]} soul, and a {data["risingSign"]} spark'
    draw_centered_text(draw, line, center_x=1000, center_y=1000, font=font)
    return img


def overlay_superpowers(img, data):
    """Page 17 — the 3 superpower phrases. Position is an
    estimate — confirm against the real page once a test PDF is
    generated."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 60)
    lines = [data["sunSuperpower"], data["moonSuperpower"], data["risingSuperpower"]]
    y = 800
    for line in lines:
        draw_centered_text(draw, line, center_x=1000, center_y=y, font=font)
        y += 140
    return img


def overlay_final_page(img, data):
    """Page 23 — closing page with the child's name."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_HEADLINE, 100)
    draw_centered_text(draw, data["childName"], center_x=1000, center_y=1700, font=font)
    return img


# Maps each dynamic page's label to its overlay function, so the
# main loop below can apply the right one automatically.
OVERLAY_MAP = {
    "Welcome (name)": overlay_welcome,
    "Birthday": overlay_birthday,
    "Magic of You": overlay_magic_of_you,
    "Your Superpowers": overlay_superpowers,
    "Final page (name)": overlay_final_page,
}


@app.route("/generate-pdf", methods=["POST"])
def generate_pdf():
    data = request.get_json()

    if not data or "pages" not in data:
        return jsonify({"error": "Missing 'pages' in request body"}), 400

    images = []
    for page in data["pages"]:
        img = fetch_image(page["url"])
        overlay_fn = OVERLAY_MAP.get(page["label"])
        if overlay_fn:
            img = overlay_fn(img, data)
        images.append(img)

    # Assemble into one multi-page PDF
    output_filename = f"book-{uuid.uuid4().hex[:10]}.pdf"
    output_path = os.path.join("/tmp", output_filename)
    images[0].save(output_path, save_all=True, append_images=images[1:])

    # ------------------------------------------------------------
    # STORAGE NOTE: this starter version saves the PDF to local
    # /tmp storage, which works for testing but disappears when
    # the server restarts, and isn't reachable by Lulu's API.
    # For production, upload output_path's contents to somewhere
    # with a permanent public URL right here — e.g. back to
    # Shopify Files via their API, or a simple object storage
    # service (S3, Cloudflare R2, etc.) — and return that real
    # URL instead of the placeholder below.
    # ------------------------------------------------------------
    return jsonify({
        "pdf_url": f"PLACEHOLDER — upload {output_filename} to permanent storage and return its real URL here",
        "pages_rendered": len(images),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))