"""
THE SKY THAT MADE YOU — Book PDF Assembly Service (v3)
====================================================================
Memory-efficient version that uploads the finished PDF to
Cloudflare R2 for permanent storage, returning a real, public URL.
====================================================================
"""

from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
import requests
from io import BytesIO
import os
import uuid
import shutil
import img2pdf
import boto3
from botocore.config import Config

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME")
R2_PUBLIC_URL = os.environ.get("R2_PUBLIC_URL")

_r2_client = None


def get_r2_client():
    global _r2_client
    if _r2_client is None:
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=R2_ACCESS_KEY_ID,
            aws_secret_access_key=R2_SECRET_ACCESS_KEY,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _r2_client


app = Flask(__name__)

PAGE_SIZE = 2000

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FONT_HEADLINE = os.path.join(FONT_DIR, "Baloo2-Bold.ttf")
FONT_BODY = os.path.join(FONT_DIR, "Nunito-Bold.ttf")

PURPLE = (108, 74, 182)
NAVY = (20, 38, 83)
CORAL = (230, 126, 90)


# --------------------------------------------------------------------
# TEXT OVERLAYS
# --------------------------------------------------------------------
# All positions are in pixels on the 2000 x 2000 page.
#   x      = horizontal centre of the blank (or left edge, for left-aligned text)
#   y      = vertical centre of the blank / icon the text sits in
#   size   = starting font size; text shrinks automatically if it is
#            wider than max_w, so long names and cities never spill out
#   max_w  = widest the text is allowed to be
# To nudge something: bigger x moves right, bigger y moves down.

WELCOME_NAME = {"x": 1000, "baseline": 405, "size": 190, "max_w": 1500}

BIRTHDAY_DOB = {"x": 1000, "y": 298, "size": 96, "max_w": 1120}
BIRTHDAY_TOB = {"x": 1083, "y": 518, "size": 72, "max_w": 300}
BIRTHDAY_CITY = {"x": 1018, "y": 612, "size": 70, "max_w": 640}

# The three pills on this page are not lined up with each other,
# so each one has its own centre. Here y is the exact middle of the
# pill, and each sign is centred on its own letter shapes (so a word
# with a tail, like Virgo, sits evenly between top and bottom).
MAGIC_SUN = {"x": 1280, "y": 947, "size": 68, "max_w": 385}
MAGIC_MOON = {"x": 1247, "y": 1059, "size": 68, "max_w": 385}
MAGIC_RISING = {"x": 1356, "y": 1172, "size": 68, "max_w": 385}

# Left-aligned, starting just right of the heart / moon / star icons.
SUPERPOWER_LEFT_X = 580
SUPERPOWER_SIZE = 76
SUPERPOWER_MAX_W = 1100
SUPERPOWER_SUN_Y = 930
SUPERPOWER_MOON_Y = 1046
SUPERPOWER_RISING_Y = 1176

# The name sits above "A story written in the stars", as a headline.
FINAL_NAME = {"x": 986, "baseline": 1425, "size": 180, "max_w": 1200}

MIN_FONT_SIZE = 28


def load_font(path, size):
    # Fail loudly if a font file is missing. A silent fallback to
    # Pillow's default font would produce a book that looks wrong
    # and still gets sent to print.
    if not os.path.exists(path):
        raise FileNotFoundError(f"Font file not found: {path}")
    return ImageFont.truetype(path, size)


def fit_font(path, text, size, max_w):
    """Largest font at or below `size` that keeps `text` within max_w."""
    font = load_font(path, size)
    while size > MIN_FONT_SIZE and font.getlength(text) > max_w:
        size -= 2
        font = load_font(path, size)
    return font


def cap_height(font):
    return -font.getbbox("H", anchor="ls")[1]


def clean(value):
    return str(value or "").strip()


def fetch_image(url):
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    img = Image.open(BytesIO(response.content)).convert("RGB")
    if img.size != (PAGE_SIZE, PAGE_SIZE):
        img = img.resize((PAGE_SIZE, PAGE_SIZE), Image.LANCZOS)
    return img


def draw_on_baseline(draw, text, x, baseline, font, fill, align="center"):
    """Draw text sitting on a fixed baseline. Because the baseline is
    fixed, letters with tails (g, y, p) or tall strokes never push the
    text up or down from one book to the next."""
    anchor = "ms" if align == "center" else "ls"
    draw.text((x, baseline), text, font=font, fill=fill, anchor=anchor)


def draw_in_blank(draw, text, spot, font_path, fill, align="center",
                  centre_on="capitals"):
    """Draw text vertically centred on spot['y'] (the middle of a pill,
    blank or icon), shrinking it if needed to fit spot['max_w'].

    centre_on="capitals": every line sits on the same baseline, lined
        up with the printed sentence around it.
    centre_on="letters":  the word's actual shapes (tails included)
        are centred, so it looks evenly spaced inside a visible pill.
    """
    text = clean(text)
    if not text:
        return
    font = fit_font(font_path, text, spot["size"], spot["max_w"])
    if centre_on == "letters":
        _, top, _, bottom = font.getbbox(text, anchor="ls")
        baseline = spot["y"] - (top + bottom) / 2
    else:
        baseline = spot["y"] + cap_height(font) / 2
    draw_on_baseline(draw, text, spot["x"], baseline, font, fill, align)


def draw_name(draw, text, spot, fill=PURPLE):
    text = clean(text)
    if not text:
        return
    font = fit_font(FONT_HEADLINE, text, spot["size"], spot["max_w"])
    draw_on_baseline(draw, text, spot["x"], spot["baseline"], font, fill)


def overlay_welcome(img, data):
    draw = ImageDraw.Draw(img)
    draw_name(draw, data["childName"], WELCOME_NAME)
    return img


def overlay_birthday(img, data):
    draw = ImageDraw.Draw(img)
    draw_in_blank(draw, data["dobDisplay"], BIRTHDAY_DOB, FONT_BODY, PURPLE)
    draw_in_blank(draw, data["tobDisplay"], BIRTHDAY_TOB, FONT_BODY, PURPLE)
    draw_in_blank(draw, data["city"], BIRTHDAY_CITY, FONT_BODY, PURPLE)
    return img


def overlay_magic_of_you(img, data):
    draw = ImageDraw.Draw(img)
    draw_in_blank(draw, data["sunSign"], MAGIC_SUN, FONT_BODY, NAVY,
                  centre_on="letters")
    draw_in_blank(draw, data["moonSign"], MAGIC_MOON, FONT_BODY, NAVY,
                  centre_on="letters")
    draw_in_blank(draw, data["risingSign"], MAGIC_RISING, FONT_BODY, NAVY,
                  centre_on="letters")
    return img


def overlay_superpowers(img, data):
    draw = ImageDraw.Draw(img)
    rows = [
        (data["sunSuperpower"], SUPERPOWER_SUN_Y, NAVY),
        (data["moonSuperpower"], SUPERPOWER_MOON_Y, PURPLE),
        (data["risingSuperpower"], SUPERPOWER_RISING_Y, CORAL),
    ]
    for text, y, colour in rows:
        spot = {"x": SUPERPOWER_LEFT_X, "y": y,
                "size": SUPERPOWER_SIZE, "max_w": SUPERPOWER_MAX_W}
        draw_in_blank(draw, text, spot, FONT_BODY, colour, align="left")
    return img


def overlay_final_page(img, data):
    draw = ImageDraw.Draw(img)
    draw_name(draw, data["childName"], FINAL_NAME)
    return img


OVERLAY_MAP = {
    "Welcome (name)": overlay_welcome,
    "Birthday": overlay_birthday,
    "Magic of You": overlay_magic_of_you,
    "Your Superpowers": overlay_superpowers,
    "Final page (name)": overlay_final_page,
}


def upload_pdf_to_r2(pdf_path, filename):
    client = get_r2_client()
    client.upload_file(
        pdf_path,
        R2_BUCKET_NAME,
        filename,
        ExtraArgs={"ContentType": "application/pdf"},
    )
    return f"{R2_PUBLIC_URL}/{filename}"


@app.route("/generate-pdf", methods=["POST"])
def generate_pdf():
    data = request.get_json()

    if not data or "pages" not in data:
        return jsonify({"error": "Missing 'pages' in request body"}), 400

    job_id = uuid.uuid4().hex[:10]
    temp_dir = os.path.join("/tmp", f"job-{job_id}")
    os.makedirs(temp_dir, exist_ok=True)

    page_paths = []

    try:
        for i, page in enumerate(data["pages"]):
            img = fetch_image(page["url"])
            overlay_fn = OVERLAY_MAP.get(page["label"])
            if overlay_fn:
                img = overlay_fn(img, data)

            page_path = os.path.join(temp_dir, f"page-{i:03d}.jpg")
            img.save(page_path, "JPEG", quality=90)
            page_paths.append(page_path)

            img.close()
            del img

        output_filename = f"book-{job_id}.pdf"
        output_path = os.path.join("/tmp", output_filename)

        with open(output_path, "wb") as f:
            f.write(img2pdf.convert(page_paths))

        pdf_url = upload_pdf_to_r2(output_path, output_filename)

        return jsonify({
            "pdf_url": pdf_url,
            "pages_rendered": len(page_paths),
        })

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route("/download/<filename>", methods=["GET"])
def download(filename):
    path = os.path.join("/tmp", filename)
    if not os.path.exists(path):
        return jsonify({"error": "File not found — the server may have restarted since it was generated. Re-run /generate-pdf."}), 404
    return send_file(path, mimetype="application/pdf", as_attachment=True, download_name=filename)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
