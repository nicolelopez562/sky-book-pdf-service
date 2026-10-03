"""
THE SKY THAT MADE YOU — Book PDF Assembly Service
====================================================================
Receives a JSON payload (the output of the Zapier Code step that
builds the 26-page list + overlay text), downloads each page image,
draws the required text overlays on top of the 5 dynamic pages, and
assembles everything into one finished PDF.

ENDPOINT: POST /generate-pdf
BODY: the full JSON object the Zapier Code step already outputs
RETURNS: { "pdf_url": "..." }
====================================================================
"""

from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
import requests
from io import BytesIO
import os
import uuid

app = Flask(__name__)

PAGE_SIZE = 2000

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FONT_HEADLINE = os.path.join(FONT_DIR, "Baloo2-Bold.ttf")
FONT_BODY = os.path.join(FONT_DIR, "Nunito-Bold.ttf")

PURPLE = (108, 74, 182)   # #6C4AB6
NAVY = (20, 38, 83)       # #142653
CORAL = (230, 126, 90)    # adjust once compared to the real page


def load_font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def fetch_image(url):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    img = Image.open(BytesIO(response.content)).convert("RGB")
    if img.size != (PAGE_SIZE, PAGE_SIZE):
        img = img.resize((PAGE_SIZE, PAGE_SIZE), Image.LANCZOS)
    return img


def draw_centered_text(draw, text, center_x, center_y, font, fill=PURPLE):
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text((center_x - text_w / 2, center_y - text_h / 2), text, font=font, fill=fill)


# ----------------------------------------------------------------
# OVERLAY POSITIONS — estimates based on the book's layout. Generate
# a real test PDF, compare against the actual Canva pages, and
# adjust these numbers until they match.
# ----------------------------------------------------------------

def overlay_welcome(img, data):
    """Page 5 — 'Hi [Name]' page."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_HEADLINE, 160)
    draw_centered_text(draw, data["childName"], center_x=820, center_y=330, font=font)
    return img


def overlay_birthday(img, data):
    """Page 6 — birthday reveal with time/city pills."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 56)
    draw_centered_text(draw, data["tobDisplay"], center_x=1360, center_y=640, font=font)
    draw_centered_text(draw, data["city"], center_x=1360, center_y=760, font=font)
    return img


def overlay_magic_of_you(img, data):
    """Page 16 — three lines, each with its own sign name dropped
    into a colored pill: 'the heart of a [Sun]', 'the soul of a
    [Moon]', 'and the spark of a [Rising]'. Each sign name is
    centered within its own pill shape, not across the page."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 60)

    pill_center_x = 1300
    draw_centered_text(draw, data["sunSign"], center_x=pill_center_x, center_y=950, font=font, fill=NAVY)
    draw_centered_text(draw, data["moonSign"], center_x=pill_center_x, center_y=1060, font=font, fill=NAVY)
    draw_centered_text(draw, data["risingSign"], center_x=pill_center_x, center_y=1170, font=font, fill=NAVY)
    return img


def overlay_superpowers(img, data):
    """Page 17 — three lines, each starting right after a fixed
    icon already baked into the background (heart/Sun, moon/Moon,
    sparkle/Rising). Text is left-aligned, with a distinct color
    per line matching the real page."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 50)

    text_left_x = 760

    draw.text((text_left_x, 820), data["sunSuperpower"], font=font, fill=NAVY)
    draw.text((text_left_x, 920), data["moonSuperpower"], font=font, fill=PURPLE)
    draw.text((text_left_x, 1020), data["risingSuperpower"], font=font, fill=CORAL)

    return img


def overlay_final_page(img, data):
    """Page 23 — closing page with the child's name."""
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_HEADLINE, 100)
    draw_centered_text(draw, data["childName"], center_x=1000, center_y=1700, font=font)
    return img


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

    output_filename = f"book-{uuid.uuid4().hex[:10]}.pdf"
    output_path = os.path.join("/tmp", output_filename)
    images[0].save(output_path, save_all=True, append_images=images[1:])

    # STORAGE NOTE: saves locally for now — swap this for permanent
    # storage (Shopify Files, S3, etc.) before going live.
    return jsonify({
        "pdf_url": f"PLACEHOLDER — upload {output_filename} to permanent storage and return its real URL here",
        "pages_rendered": len(images),
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
