"""
THE SKY THAT MADE YOU — Book PDF Assembly Service (v4)
====================================================================
Memory-efficient version that uploads the finished PDF to
Cloudflare R2 for permanent storage, returning a real, public URL.

Two things it makes:
  /generate-pdf    the 26-page inside of the book
  /generate-cover  the printed cover (back, spine, front) with the
                   child's name stamped on the front
====================================================================
"""

from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
import requests
from io import BytesIO
import os
import uuid
import shutil
import time
import unicodedata
import img2pdf
import boto3
import uharfbuzz as hb
from botocore.config import Config
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

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

PAGE_SIZE = 2000          # each page image, in pixels

# Printed size of each inside page, in inches. The book trims to
# 8.5 x 8.5 in; Lulu needs 0.125 in of extra artwork on every side,
# so the page is 8.75 x 8.75 in (the same size as the Canva design).
PAGE_INCHES = 8.75

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
FONT_HEADLINE = os.path.join(FONT_DIR, "Baloo2-Bold.ttf")
FONT_BODY = os.path.join(FONT_DIR, "Nunito-Bold.ttf")
FONT_COVER = os.path.join(FONT_DIR, "Baloo-Regular.ttf")  # the cover's own font

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


def fresh_url(url):
    """Ask for the current copy of a file, not a cached one.

    Shopify keeps serving an old copy of a replaced file at its plain
    address for a while. Adding a version stamp makes it hand over the
    current one. Addresses that already carry a stamp are left alone.
    """
    if "?" in url:
        return url
    return url + "?v=" + str(int(time.time()))


def fetch_bytes(url, timeout=30):
    response = requests.get(fresh_url(url), timeout=timeout)
    response.raise_for_status()
    return response.content


def fetch_image(url):
    img = Image.open(BytesIO(fetch_bytes(url, timeout=20))).convert("RGB")
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


# --------------------------------------------------------------------
# PRINTED COVER
# --------------------------------------------------------------------
# The cover is one wide PDF page (back, spine, front) designed in Canva
# at 19.25 x 10.25 in. The design is used exactly as it is; only the
# child's name is added, as real text in the cover's own font.
#
# Positions here are in PDF points (72 per inch), measured from the
# LEFT and from the BOTTOM of the page, taken from the placeholder
# name on the master cover.
#   x        = horizontal centre of the name
#   baseline = the line the letters sit on (bigger = higher up)
#   size     = full size; longer names shrink to fit max_w
#   max_w    = widest the name may be (the width of the moon)

COVER_PAGE_SIZE = (1386.0, 738.0)          # 19.25 x 10.25 in
COVER_NAME = {"x": 1035.75, "baseline": 115.03, "size": 60.57,
              "max_w": 380.0, "min_size": 24.0}
COVER_NAME_CMYK = (0.945, 0.745, 0.027, 0.604)   # the cover's navy

_cover_hb_font = None
_cover_font_registered = False


def _load_cover_font():
    global _cover_hb_font, _cover_font_registered
    if not os.path.exists(FONT_COVER):
        raise FileNotFoundError(f"Font file not found: {FONT_COVER}")
    if _cover_hb_font is None:
        with open(FONT_COVER, "rb") as f:
            _cover_hb_font = hb.Font(hb.Face(f.read()))
    if not _cover_font_registered:
        pdfmetrics.registerFont(TTFont("CoverName", FONT_COVER))
        _cover_font_registered = True
    return _cover_hb_font


def shape_cover_name(text):
    """Work out where each letter sits, with the font's own letter-pair
    spacing (kerning), the same way Canva sets it.

    Returns ([(letter, x)], total_width) for a font size of 1.
    """
    font = _load_cover_font()
    upem = font.face.upem
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {"kern": True, "liga": False, "clig": False, "dlig": False})

    infos, positions = buf.glyph_infos, buf.glyph_positions
    if len(infos) != len(text):
        raise ValueError(f"The name '{text}' has characters the cover font can't set cleanly.")

    letters = []
    pen = 0
    for i, (info, pos) in enumerate(zip(infos, positions)):
        if info.codepoint == 0 or info.cluster != i:
            raise ValueError(
                f"The name '{text}' contains a character the cover font can't print: '{text[i]}'")
        letters.append((text[i], (pen + pos.x_offset) / upem))
        pen += pos.x_advance
    return letters, pen / upem


def _name_slot_is_empty(page):
    """True if the chosen template page has no text where the name goes.
    Stops a name being printed on top of a leftover placeholder."""
    found = []
    spot = COVER_NAME

    def in_slot(x, y):
        return abs(y - spot["baseline"]) < 30 and abs(x - spot["x"]) < 320

    def visit(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        # Canva writes the text position straight into the text matrix;
        # other tools combine it with the page matrix. Check both.
        plain = (tm[4], tm[5])
        combined = (tm[4] * cm[0] + tm[5] * cm[2] + cm[4],
                    tm[4] * cm[1] + tm[5] * cm[3] + cm[5])
        if in_slot(*plain) or in_slot(*combined):
            found.append(text.strip())

    page.extract_text(visitor_text=visit)
    return not found


def build_cover_pdf(template_bytes, child_name, out_path):
    """Stamp the child's name onto the cover template and save it."""
    name = unicodedata.normalize("NFC", clean(child_name))
    if not name:
        raise ValueError("Child name is empty, so the cover can't be made.")

    reader = PdfReader(BytesIO(template_bytes))
    # A template may hold a sample page with a placeholder name first;
    # the clean, name-free cover is always the LAST page.
    page = reader.pages[-1]

    width, height = float(page.mediabox.width), float(page.mediabox.height)
    if abs(width - COVER_PAGE_SIZE[0]) > 1 or abs(height - COVER_PAGE_SIZE[1]) > 1:
        raise ValueError(
            f"Cover template is {width / 72:.2f} x {height / 72:.2f} in, "
            f"expected {COVER_PAGE_SIZE[0] / 72:.2f} x {COVER_PAGE_SIZE[1] / 72:.2f} in.")
    if not _name_slot_is_empty(page):
        raise ValueError(
            "The cover template's last page already has a name where the child's "
            "name goes. Export the cover without the placeholder name.")

    letters, unit_width = shape_cover_name(name)
    spot = COVER_NAME
    size = spot["size"]
    if unit_width * size > spot["max_w"]:
        size = max(spot["min_size"], spot["max_w"] / unit_width)
    left = spot["x"] - unit_width * size / 2

    layer = BytesIO()
    # Start the layer in the cover font so no other (un-embedded) font
    # is ever referenced in the print file.
    c = rl_canvas.Canvas(layer, pagesize=(width, height),
                         initialFontName="CoverName", initialFontSize=size)
    c.setFillColorCMYK(*COVER_NAME_CMYK)
    c.setFont("CoverName", size)
    for letter, x in letters:
        c.drawString(left + x * size, spot["baseline"], letter)
    c.save()
    layer.seek(0)

    page.merge_page(PdfReader(layer).pages[0])
    writer = PdfWriter()
    writer.add_page(page)
    with open(out_path, "wb") as f:
        writer.write(f)
    return {"font_size": round(size, 2), "name_width": round(unit_width * size, 1)}


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

        page_pt = img2pdf.in_to_pt(PAGE_INCHES)
        layout = img2pdf.get_layout_fun((page_pt, page_pt))
        with open(output_path, "wb") as f:
            f.write(img2pdf.convert(page_paths, layout_fun=layout))

        pdf_url = upload_pdf_to_r2(output_path, output_filename)

        return jsonify({
            "pdf_url": pdf_url,
            "pages_rendered": len(page_paths),
        })

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route("/generate-cover", methods=["POST"])
def generate_cover():
    data = request.get_json()

    if not data or not data.get("coverTemplateUrl"):
        return jsonify({"error": "Missing 'coverTemplateUrl' in request body"}), 400
    if not clean(data.get("childName")):
        return jsonify({"error": "Missing 'childName' in request body"}), 400

    job_id = uuid.uuid4().hex[:10]
    output_filename = f"cover-{job_id}.pdf"
    output_path = os.path.join("/tmp", output_filename)

    try:
        template = fetch_bytes(data["coverTemplateUrl"], timeout=60)
        details = build_cover_pdf(template, data["childName"], output_path)
        cover_url = upload_pdf_to_r2(output_path, output_filename)
        return jsonify({"cover_pdf_url": cover_url, **details})
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)


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
