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


def overlay_welcome(img, data):
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_HEADLINE, 160)
    draw_centered_text(draw, data["childName"], center_x=820, center_y=330, font=font)
    return img


def overlay_birthday(img, data):
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 56)
    draw_centered_text(draw, data["tobDisplay"], center_x=1360, center_y=640, font=font)
    draw_centered_text(draw, data["city"], center_x=1360, center_y=760, font=font)
    return img


def overlay_magic_of_you(img, data):
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 60)
    pill_center_x = 1300
    draw_centered_text(draw, data["sunSign"], center_x=pill_center_x, center_y=950, font=font, fill=NAVY)
    draw_centered_text(draw, data["moonSign"], center_x=pill_center_x, center_y=1060, font=font, fill=NAVY)
    draw_centered_text(draw, data["risingSign"], center_x=pill_center_x, center_y=1170, font=font, fill=NAVY)
    return img


def overlay_superpowers(img, data):
    draw = ImageDraw.Draw(img)
    font = load_font(FONT_BODY, 50)
    text_left_x = 760
    draw.text((text_left_x, 820), data["sunSuperpower"], font=font, fill=NAVY)
    draw.text((text_left_x, 920), data["moonSuperpower"], font=font, fill=PURPLE)
    draw.text((text_left_x, 1020), data["risingSuperpower"], font=font, fill=CORAL)
    return img


def overlay_final_page(img, data):
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
