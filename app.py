"""
THE SKY THAT MADE YOU — Book PDF Assembly Service (v6.1)
====================================================================
Memory-efficient version that uploads the finished PDF to
Cloudflare R2 for permanent storage, returning a real, public URL.

What it makes:
  /generate-pdf    the 26-page inside of the book (waits until done)
  /generate-cover  the printed cover (back, spine, front) with the
                   child's name stamped on the front (waits until done)
  /build-book      both of the above for one book, for Zapier: it
                   answers straight away, builds in the background,
                   then posts both file links to a Zapier "catch hook".
                   (Zapier gives up on a step after about 30 seconds,
                   which is not long enough to build a whole book.)
                   When the last book of an order is built, the whole
                   order is sent to Lulu as one print job (one box).
  /send-order      sends an order that was held back (for example a
                   book whose birth place needed checking) to Lulu.
====================================================================
"""

from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
import requests
from io import BytesIO
import os
import uuid
import shutil
import time
import json
import base64
import hashlib
import hmac
import threading
import unicodedata
import img2pdf
import boto3
import uharfbuzz as hb
from botocore.config import Config
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# Settings from Render > Environment. Names are matched ignoring stray
# spaces and upper/lower case, and values are trimmed, so a copy-paste
# slip (like a space after the name) doesn't silently hide a setting.
_SETTINGS = {k.strip().upper(): v.strip() for k, v in os.environ.items()}


def setting(name, default=""):
    value = _SETTINGS.get(name.upper(), "")
    return value if value else default


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
# The Welcome page speaks to the child ("Justine, the day you were
# born..."), so the name is followed by a comma. Set to False to drop it.
WELCOME_NAME_COMMA = False

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
SUPERPOWER_RISING_Y = 1166

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
    name = clean(data["childName"])
    if not name:
        return img
    if not WELCOME_NAME_COMMA:
        draw_name(draw, name, WELCOME_NAME)
        return img
    spot = WELCOME_NAME
    font = fit_font(FONT_HEADLINE, name + ",", spot["size"], spot["max_w"])
    # Keep the name itself centred on the page; the comma hangs to its right.
    left = spot["x"] - font.getlength(name) / 2
    draw_on_baseline(draw, name + ",", left, spot["baseline"], font, PURPLE, align="left")
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


def make_interior(data):
    """Builds the 26-page inside of the book and uploads it. Returns its details."""
    job_id = uuid.uuid4().hex[:10]
    temp_dir = os.path.join("/tmp", f"job-{job_id}")
    os.makedirs(temp_dir, exist_ok=True)
    output_filename = f"book-{job_id}.pdf"
    output_path = os.path.join("/tmp", output_filename)
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

        page_pt = img2pdf.in_to_pt(PAGE_INCHES)
        layout = img2pdf.get_layout_fun((page_pt, page_pt))
        with open(output_path, "wb") as f:
            f.write(img2pdf.convert(page_paths, layout_fun=layout))

        pdf_url = upload_pdf_to_r2(output_path, output_filename)
        return {"pdf_url": pdf_url, "pages_rendered": len(page_paths)}

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(output_path):
            os.remove(output_path)


def make_cover(template_url, child_name):
    """Stamps the name on a cover template and uploads it. Returns its details."""
    job_id = uuid.uuid4().hex[:10]
    output_filename = f"cover-{job_id}.pdf"
    output_path = os.path.join("/tmp", output_filename)
    try:
        template = fetch_bytes(template_url, timeout=60)
        details = build_cover_pdf(template, child_name, output_path)
        cover_url = upload_pdf_to_r2(output_path, output_filename)
        return {"cover_pdf_url": cover_url, **details}
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)


@app.route("/generate-pdf", methods=["POST"])
def generate_pdf():
    data = request.get_json()

    if not data or "pages" not in data:
        return jsonify({"error": "Missing 'pages' in request body"}), 400

    return jsonify(make_interior(data))


@app.route("/generate-cover", methods=["POST"])
def generate_cover():
    data = request.get_json()

    if not data or not data.get("coverTemplateUrl"):
        return jsonify({"error": "Missing 'coverTemplateUrl' in request body"}), 400
    if not clean(data.get("childName")):
        return jsonify({"error": "Missing 'childName' in request body"}), 400

    try:
        return jsonify(make_cover(data["coverTemplateUrl"], data["childName"]))
    except ValueError as e:
        return jsonify({"error": str(e)}), 422


# --------------------------------------------------------------------
# /build-book — one whole book, built in the background for Zapier
# --------------------------------------------------------------------
# Where the ten printed-cover templates live (Print_Cover_A.pdf ... J).
COVER_TEMPLATE_BASE = "https://cdn.shopify.com/s/files/1/0827/5850/0600/files/Print_Cover_"

# Page images and cover templates may only come from these addresses,
# and finished-book messages may only be sent to Zapier.
ALLOWED_ASSET_PREFIXES = tuple(
    setting("ALLOWED_ASSET_PREFIXES", "https://cdn.shopify.com/").split(","))
ALLOWED_CALLBACK_PREFIXES = tuple(
    setting("ALLOWED_CALLBACK_PREFIXES", "https://hooks.zapier.com/").split(","))

# Optional password. When BUILD_SECRET is set on Render, every
# /build-book request must carry the same value in its "secret" field.
BUILD_SECRET = setting("BUILD_SECRET", "")

# Books are built one at a time so a 5-book order can't run the
# server out of memory; the others wait their turn.
BUILD_SLOTS = threading.BoundedSemaphore(int(setting("BUILD_AT_ONCE", "1")))


def _field(data, name):
    return clean(data.get(name)) if data else ""


def _short(err):
    text = " ".join(str(err).split())
    return text[:300] if text else err.__class__.__name__


def post_callback(url, payload, tries=4):
    """Sends the finished-book message to Zapier, retrying if Zapier is busy."""
    for attempt in range(tries):
        try:
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code < 500:
                return r.status_code < 400
        except requests.RequestException as e:
            app.logger.warning("Callback attempt %s failed: %s", attempt + 1, e)
        time.sleep(5 * (attempt + 1))
    app.logger.error("Gave up sending the finished-book message for %s", payload.get("orderLabel"))
    return False


def run_build(job):
    result = {
        "jobId": job["jobId"],
        "notionPageId": job["notionPageId"],
        "orderLabel": job["orderLabel"],
        "childName": job["childName"],
        "coverId": job["coverId"],
        "interiorPdfUrl": "",
        "interiorPages": 0,
        "coverPdfUrl": "",
        "status": "ok",
        "filesNote": "",
    }
    notes = []
    with BUILD_SLOTS:
        try:
            interior = make_interior(job["book"])
            result["interiorPdfUrl"] = interior["pdf_url"]
            result["interiorPages"] = interior["pages_rendered"]
        except Exception as e:  # noqa: BLE001 - every failure must reach Notion
            app.logger.exception("Interior failed for %s", job["orderLabel"])
            notes.append("Inside pages failed: " + _short(e))
        try:
            cover = make_cover(job["coverTemplateUrl"], job["childName"])
            result["coverPdfUrl"] = cover["cover_pdf_url"]
        except Exception as e:  # noqa: BLE001
            app.logger.exception("Cover failed for %s", job["orderLabel"])
            notes.append("Cover failed: " + _short(e))
    if notes:
        result["status"] = "error"
        result["filesNote"] = " | ".join(notes)
    result["luluJobId"] = ""
    result["notionStatus"] = "Needs attention" if notes else ""
    post_callback(job["callbackUrl"], result)

    # Remember this book, then send the order to Lulu if it was the last one.
    order_name = job.get("orderName")
    if not order_name or notes:
        return
    try:
        save_record(order_prefix(order_name) + f"book-{job['bookNumber']}.json", {
            "bookNumber": job["bookNumber"], "printCount": job["printCount"],
            "quantity": job["quantity"], "status": job["bookStatus"],
            "childName": job["childName"], "coverId": job["coverId"],
            "notionPageId": job["notionPageId"], "orderLabel": job["orderLabel"],
            "interiorPdfUrl": result["interiorPdfUrl"], "coverPdfUrl": result["coverPdfUrl"],
            "interiorPages": result["interiorPages"], "ship": job["ship"],
            "callbackUrl": job["callbackUrl"]})
        send_order(order_name, job["ship"], job["callbackUrl"])
    except Exception as e:  # noqa: BLE001
        app.logger.exception("Could not record or send order %s", order_name)
        result["filesNote"] = "Files ready, but the order could not be sent to Lulu: " + _short(e)
        result["notionStatus"] = "Needs attention"
        post_callback(job["callbackUrl"], result)


# --------------------------------------------------------------------
# LULU — one print job per order
# --------------------------------------------------------------------
# Settings (Render > Environment). Nothing is sent to Lulu until both
# LULU_CLIENT_KEY and LULU_CLIENT_SECRET are set. LULU_ENV stays
# "sandbox" (test orders, never printed or charged) until it is changed
# to "production".
LULU_CLIENT_KEY = setting("LULU_CLIENT_KEY", "")
LULU_CLIENT_SECRET = setting("LULU_CLIENT_SECRET", "")
LULU_ENV = setting("LULU_ENV", "sandbox").strip().lower()
LULU_BASE = "https://api.lulu.com" if LULU_ENV == "production" else "https://api.sandbox.lulu.com"
LULU_POD_PACKAGE_ID = setting("LULU_POD_PACKAGE_ID", "0850X0850.FC.PRE.CW.080CW444.GXX")
LULU_CONTACT_EMAIL = setting("LULU_CONTACT_EMAIL", "hello@theskythatmadeyou.com")
LULU_DEFAULT_PHONE = setting("LULU_DEFAULT_PHONE", "")
# Minutes Lulu waits before printing, so a job can still be stopped.
LULU_PRODUCTION_DELAY = int(setting("LULU_PRODUCTION_DELAY", "1440"))
# Shopify shipping option name -> Lulu shipping level.
LULU_SHIPPING_MAP = json.loads(setting("LULU_SHIPPING_MAP", '{"Standard": "GROUND"}'))
LULU_DEFAULT_SHIPPING = setting("LULU_DEFAULT_SHIPPING", "GROUND")
BOOK_TITLE = "The Sky That Made You"
READY_STATUS = "Signs calculated"

_lulu_token = {"value": "", "expires": 0.0}
ORDER_LOCK = threading.Lock()


def lulu_enabled():
    return bool(LULU_CLIENT_KEY and LULU_CLIENT_SECRET)


def lulu_token():
    if _lulu_token["value"] and time.time() < _lulu_token["expires"] - 60:
        return _lulu_token["value"]
    basic = base64.b64encode(f"{LULU_CLIENT_KEY}:{LULU_CLIENT_SECRET}".encode()).decode()
    r = requests.post(
        f"{LULU_BASE}/auth/realms/glasstree/protocol/openid-connect/token",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Lulu sign-in failed (status {r.status_code}): {r.text[:200]}")
    body = r.json()
    _lulu_token["value"] = body["access_token"]
    _lulu_token["expires"] = time.time() + float(body.get("expires_in", 300))
    return _lulu_token["value"]


def order_prefix(order_name):
    """Storage folder for an order's records. Not guessable without the
    server's secret, because the records include the shipping address."""
    key = (BUILD_SECRET or LULU_CLIENT_SECRET or "sky").encode()
    tag = hmac.new(key, order_name.encode(), hashlib.sha256).hexdigest()[:24]
    digits = "".join(ch for ch in order_name if ch.isalnum()) or "order"
    return f"orders/{digits}-{tag}/"


def save_record(key, data):
    get_r2_client().put_object(Bucket=R2_BUCKET_NAME, Key=key,
                               Body=json.dumps(data).encode(), ContentType="application/json")


def load_record(key):
    try:
        obj = get_r2_client().get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return json.loads(obj["Body"].read())
    except Exception:  # noqa: BLE001 - "not there yet"
        return None


def order_books(order_name):
    prefix = order_prefix(order_name)
    out = []
    listing = get_r2_client().list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix + "book-")
    for item in listing.get("Contents", []):
        rec = load_record(item["Key"])
        if rec:
            out.append(rec)
    return sorted(out, key=lambda b: b.get("bookNumber", 0))


def lulu_shipping_level(method):
    return LULU_SHIPPING_MAP.get(clean(method), LULU_DEFAULT_SHIPPING)


def create_lulu_job(order_name, books, ship):
    """One print job for the whole order. Returns Lulu's reply."""
    phone = clean(ship.get("phone")) or LULU_DEFAULT_PHONE
    address = {
        "name": clean(ship.get("name")),
        "street1": clean(ship.get("street1")),
        "street2": clean(ship.get("street2")),
        "city": clean(ship.get("city")),
        "state_code": clean(ship.get("stateCode")),
        "postcode": clean(ship.get("postcode")),
        "country_code": clean(ship.get("countryCode")).upper() or "US",
        "phone_number": phone,
        "email": clean(ship.get("email")),
    }
    address = {k: v for k, v in address.items() if v}
    missing = [k for k in ("name", "street1", "city", "postcode", "phone_number") if k not in address]
    if missing:
        raise RuntimeError("Shipping address is missing: " + ", ".join(missing))
    body = {
        "external_id": order_name,
        "contact_email": LULU_CONTACT_EMAIL,
        "shipping_level": lulu_shipping_level(ship.get("method")),
        "production_delay": LULU_PRODUCTION_DELAY,
        "shipping_address": address,
        "line_items": [{
            "external_id": f"{order_name} book {b['bookNumber']}",
            "title": f"{BOOK_TITLE} - {b['childName']}",
            "quantity": int(b.get("quantity") or 1),
            "printable_normalization": {
                "pod_package_id": LULU_POD_PACKAGE_ID,
                "interior": {"source_url": b["interiorPdfUrl"]},
                "cover": {"source_url": b["coverPdfUrl"]},
            },
        } for b in books],
    }
    r = requests.post(f"{LULU_BASE}/print-jobs/",
                      headers={"Authorization": f"Bearer {lulu_token()}",
                               "Content-Type": "application/json"},
                      json=body, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Lulu refused the print job (status {r.status_code}): {r.text[:400]}")
    return r.json()


def lulu_job_status(reply):
    status = reply.get("status")
    if isinstance(status, dict):
        return clean(status.get("name"))
    return clean(status)


def send_order(order_name, ship, callback_url, force=False):
    """Sends the order to Lulu if every book is built, then tells Notion
    about every book on the order. Safe to call more than once."""
    prefix = order_prefix(order_name)
    with ORDER_LOCK:
        sent = load_record(prefix + "lulu.json")
        books = order_books(order_name)
        expected = max([int(b.get("printCount") or 0) for b in books] or [0])
        if sent:
            return {"state": "already_sent", "luluJobId": sent.get("luluJobId", "")}
        if not books:
            return {"state": "waiting", "built": 0, "expected": expected}
        if expected and len(books) < expected:
            return {"state": "waiting", "built": len(books), "expected": expected}
        flagged = [b for b in books if clean(b.get("status")) not in ("", READY_STATUS)]
        failed = [b for b in books if not (b.get("interiorPdfUrl") and b.get("coverPdfUrl"))]
        if expected == 0:
            note = "Not sent to Lulu: the Zap did not say how many books are on this order (printCount)."
            result = {"state": "held", "note": note}
        elif failed:
            note = "Not sent to Lulu: a book on this order has no files."
            result = {"state": "held", "note": note}
        elif flagged and not force:
            names = ", ".join(b["childName"] for b in flagged)
            note = (f"Not sent to Lulu yet: check the place of birth for {names}, "
                    f"then send the order with /send-order.")
            result = {"state": "held", "note": note}
        elif not lulu_enabled():
            note = "Files ready. Not sent to Lulu: Lulu keys are not set on the server."
            result = {"state": "held", "note": note}
        else:
            try:
                reply = create_lulu_job(order_name, books, ship)
                job_id = str(reply.get("id", ""))
                save_record(prefix + "lulu.json", {
                    "luluJobId": job_id, "env": LULU_ENV, "sentAt": int(time.time()),
                    "status": lulu_job_status(reply)})
                where = "Lulu sandbox (test)" if LULU_ENV != "production" else "Lulu"
                note = f"Sent to {where} as print job {job_id} with {len(books)} book(s)."
                result = {"state": "sent", "luluJobId": job_id, "note": note}
            except Exception as e:  # noqa: BLE001
                app.logger.exception("Lulu send failed for %s", order_name)
                note = "Lulu: " + _short(e)
                result = {"state": "error", "note": note}

    # Tell Notion, one message per book on the order.
    for b in books:
        msg = {
            "jobId": "", "notionPageId": b.get("notionPageId", ""),
            "orderLabel": b.get("orderLabel", ""), "childName": b.get("childName", ""),
            "coverId": b.get("coverId", ""),
            "interiorPdfUrl": b.get("interiorPdfUrl", ""), "coverPdfUrl": b.get("coverPdfUrl", ""),
            "interiorPages": b.get("interiorPages", 0),
            "status": "ok" if result["state"] == "sent" else result["state"],
            "filesNote": result["note"],
            "luluJobId": result.get("luluJobId", ""),
            "notionStatus": "Sent to printer" if result["state"] == "sent"
                            else ("Needs attention" if result["state"] == "error" else ""),
        }
        if callback_url:
            post_callback(callback_url, msg)
    return result


@app.route("/build-book", methods=["POST"])
def build_book():
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict()   # Zapier's "form" payload type
    problems = []

    if BUILD_SECRET and _field(data, "secret") != BUILD_SECRET:
        return jsonify({"error": "Wrong or missing 'secret'"}), 403

    # The book itself: the "Pdf Request Body" from the Zap's code step.
    book = data.get("book") if data else None
    if isinstance(book, str):
        try:
            book = json.loads(book)
        except ValueError:
            book = None
    if not isinstance(book, dict) or not isinstance(book.get("pages"), list) or not book["pages"]:
        problems.append("'book' is missing or is not the code step's Pdf Request Body")
        book = {}
    else:
        for page in book["pages"]:
            if not str(page.get("url", "")).startswith(ALLOWED_ASSET_PREFIXES):
                problems.append("page image not on an allowed address: " + str(page.get("url"))[:120])
                break

    child_name = _field(data, "childName") or clean(book.get("childName"))
    if not child_name:
        problems.append("'childName' is missing")

    cover_url = _field(data, "coverTemplateUrl")
    cover_id = _field(data, "coverId").upper()
    if not cover_url:
        if len(cover_id) == 1 and "A" <= cover_id <= "J":
            cover_url = f"{COVER_TEMPLATE_BASE}{cover_id}.pdf"
        else:
            problems.append(f"'coverId' should be a letter A-J, got '{cover_id}'")
    if cover_url and not cover_url.startswith(ALLOWED_ASSET_PREFIXES):
        problems.append("cover template not on an allowed address")

    callback_url = _field(data, "callbackUrl")
    if not callback_url.startswith(ALLOWED_CALLBACK_PREFIXES):
        problems.append("'callbackUrl' must be a Zapier catch hook (https://hooks.zapier.com/...)")

    if problems:
        return jsonify({"error": "; ".join(problems)}), 400

    def as_int(name, default):
        try:
            return int(float(_field(data, name) or default))
        except ValueError:
            return default

    job = {
        "orderName": _field(data, "orderName"),
        "bookNumber": as_int("bookNumber", 1),
        "printCount": as_int("printCount", 0),   # 0 = unknown: never send
        "quantity": max(as_int("quantity", 1), 1),
        "bookStatus": _field(data, "bookStatus"),
        "ship": {
            "name": _field(data, "shipName"), "street1": _field(data, "shipStreet1"),
            "street2": _field(data, "shipStreet2"), "city": _field(data, "shipCity"),
            "stateCode": _field(data, "shipStateCode"), "postcode": _field(data, "shipPostcode"),
            "countryCode": _field(data, "shipCountryCode"), "phone": _field(data, "shipPhone"),
            "email": _field(data, "customerEmail"), "method": _field(data, "shippingMethod"),
        },
        "jobId": uuid.uuid4().hex[:10],
        "book": book,
        "childName": child_name,
        "coverId": cover_id,
        "coverTemplateUrl": cover_url,
        "callbackUrl": callback_url,
        "notionPageId": _field(data, "notionPageId"),
        "orderLabel": _field(data, "orderLabel"),
    }
    threading.Thread(target=run_build, args=(job,), daemon=True).start()
    return jsonify({"accepted": True, "jobId": job["jobId"],
                    "message": "Building in the background; the links will be sent to the callback."}), 202


@app.route("/send-order", methods=["POST"])
def send_order_route():
    """Sends a held order to Lulu, e.g. after a place of birth was checked.
    Body: {"secret": "...", "orderName": "#1009"}"""
    data = request.get_json(silent=True) or {}
    if BUILD_SECRET and _field(data, "secret") != BUILD_SECRET:
        return jsonify({"error": "Wrong or missing 'secret'"}), 403
    order_name = _field(data, "orderName")
    if not order_name:
        return jsonify({"error": "Missing 'orderName' (for example #1009)"}), 400
    books = order_books(order_name)
    if not books:
        return jsonify({"error": f"No built books found for {order_name}"}), 404
    last = books[-1]
    result = send_order(order_name, last.get("ship") or {}, last.get("callbackUrl", ""), force=True)
    return jsonify(result), 200


@app.route("/check", methods=["POST"])
def check_settings():
    """Shows which settings the service can see, without showing values.
    Body: {"secret": "..."}"""
    data = request.get_json(silent=True) or {}
    if BUILD_SECRET and _field(data, "secret") != BUILD_SECRET:
        return jsonify({"error": "Wrong or missing 'secret'"}), 403
    seen = {}
    for raw_name, value in os.environ.items():
        if raw_name.strip().upper().startswith(("LULU", "BUILD", "R2_")):
            seen[repr(raw_name)] = {"length": len(value),
                                    "has_spaces_around": value != value.strip()}
    return jsonify({
        "luluKeysFound": lulu_enabled(),
        "luluEnvironment": LULU_ENV,
        "luluAddress": LULU_BASE,
        "buildSecretSet": bool(BUILD_SECRET),
        "settingsSeen": seen,
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
