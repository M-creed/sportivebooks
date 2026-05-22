import asyncio
import json
import base64
import os
import httpx
import fitz
import threading
from flask import Flask, request, jsonify
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

API_ID    = int(os.environ["TG_API_ID"])
API_HASH  = os.environ["TG_API_HASH"]
CHANNEL   = os.environ["TG_CHANNEL"]
GH_TOKEN  = os.environ["GH_TOKEN"]
GH_REPO   = os.environ["GH_REPO"]
GH_BRANCH = os.environ.get("GH_BRANCH", "main")

BOOKS_PATH = "data/books.json"
THUMBS_DIR = "data/thumbs"
BATCH_SIZE = 10

client = TelegramClient("session", API_ID, API_HASH)
app    = Flask(__name__)
http_global = None  # shared httpx client

# ════════════════════════════════════════
#  GitHub helpers
# ════════════════════════════════════════

async def gh_get_sha(http, path):
    r = await http.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{path}",
        headers={"Authorization": f"token {GH_TOKEN}"},
        params={"ref": GH_BRANCH}
    )
    return r.json().get("sha") if r.status_code == 200 else None

async def gh_upload(http, path, content_bytes, msg="update"):
    sha  = await gh_get_sha(http, path)
    body = {
        "message": msg,
        "content": base64.b64encode(content_bytes).decode(),
        "branch":  GH_BRANCH
    }
    if sha:
        body["sha"] = sha
    r = await http.put(
        f"https://api.github.com/repos/{GH_REPO}/contents/{path}",
        headers={"Authorization": f"token {GH_TOKEN}"},
        json=body
    )
    return r.status_code in (200, 201)

async def gh_load_books(http):
    r = await http.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{BOOKS_PATH}",
        headers={"Authorization": f"token {GH_TOKEN}"},
        params={"ref": GH_BRANCH}
    )
    if r.status_code == 200:
        return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    return []

async def gh_save_books(http, books, msg="update books"):
    json_bytes = json.dumps(books, ensure_ascii=False, indent=2).encode("utf-8")
    return await gh_upload(http, BOOKS_PATH, json_bytes, msg)

# ════════════════════════════════════════
#  معالجة كتاب واحد
# ════════════════════════════════════════

async def process_book(message, http):
    if not (message.media and isinstance(message.media, MessageMediaDocument)):
        return None

    doc      = message.media.document
    filename = None
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
            break

    if not (filename and filename.lower().endswith(".pdf")):
        return None

    size_mb = round(doc.size / 1_048_576, 2)
    print(f"\n📥 [{size_mb} MB] {filename}")

    # حمّل الـ PDF
    pdf_url   = None
    pdf_bytes = None
    try:
        pdf_bytes = await client.download_media(message, file=bytes)
        if pdf_bytes:
            safe     = filename.replace(" ", "_")
            pdf_path = f"data/books/{safe}"
            ok = await gh_upload(http, pdf_path, pdf_bytes, f"book: {filename}")
            if ok:
                pdf_url = f"https://raw.githubusercontent.com/{GH_REPO}/{GH_BRANCH}/{pdf_path}"
                print(f"  📚 PDF uploaded")
    except Exception as e:
        print(f"  ⚠️ PDF error: {e}")

    # thumbnail من أول صفحة
    thumb_url = None
    if pdf_bytes:
        try:
            pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            if pdf_doc.page_count > 0:
                pix       = pdf_doc[0].get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
                img_bytes = pix.tobytes("jpeg", jpg_quality=85)
                pdf_doc.close()
                thumb_path = f"{THUMBS_DIR}/{message.id}.jpg"
                ok = await gh_upload(http, thumb_path, img_bytes, f"thumb: {filename}")
                if ok:
                    thumb_url = f"https://raw.githubusercontent.com/{GH_REPO}/{GH_BRANCH}/{thumb_path}"
                    print(f"  🖼️ thumbnail uploaded")
        except Exception as e:
            print(f"  ⚠️ thumb error: {e}")

    return {
        "title"      : filename.replace(".pdf", ""),
        "filename"   : filename,
        "size_mb"    : size_mb,
        "date"       : message.date.strftime("%Y-%m-%d"),
        "caption"    : (message.message or "").strip(),
        "tg_link"    : f"https://t.me/{CHANNEL}/{message.id}",
        "message_id" : message.id,
        "thumb_url"  : thumb_url,
        "pdf_url"    : pdf_url
    }

# ════════════════════════════════════════
#  Flask API endpoints
# ════════════════════════════════════════

@app.route("/search")
def search():
    """بحث في القناة كاملة"""
    q = request.args.get("q", "").strip().lower()
    if not q or len(q) < 2:
        return jsonify({"error": "query too short"}), 400

    loop    = asyncio.new_event_loop()
    results = loop.run_until_complete(_search_channel(q))
    loop.close()
    return jsonify(results)

async def _search_channel(query):
    results = []
    async for message in client.iter_messages(CHANNEL, search=query, limit=20):
        if not (message.media and isinstance(message.media, MessageMediaDocument)):
            continue
        doc      = message.media.document
        filename = None
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                filename = attr.file_name
                break
        if not (filename and filename.lower().endswith(".pdf")):
            continue

        size_mb = round(doc.size / 1_048_576, 2)

        # هل الكتاب موجود على GitHub؟
        safe    = filename.replace(" ", "_")
        pdf_url = f"https://raw.githubusercontent.com/{GH_REPO}/{GH_BRANCH}/data/books/{safe}"

        results.append({
            "title"      : filename.replace(".pdf", ""),
            "filename"   : filename,
            "size_mb"    : size_mb,
            "date"       : message.date.strftime("%Y-%m-%d"),
            "tg_link"    : f"https://t.me/{CHANNEL}/{message.id}",
            "message_id" : message.id,
            "pdf_url"    : pdf_url
        })
    return results

@app.route("/download", methods=["POST"])
def download():
    """حمّل كتاب من تيليجرام وارفعه على GitHub"""
    data       = request.json
    message_id = data.get("message_id")
    if not message_id:
        return jsonify({"error": "message_id required"}), 400

    loop   = asyncio.new_event_loop()
    result = loop.run_until_complete(_download_and_upload(int(message_id)))
    loop.close()
    return jsonify(result)

async def _download_and_upload(message_id):
    global http_global
    try:
        message = await client.get_messages(CHANNEL, ids=message_id)
        if not message:
            return {"error": "message not found"}

        book = await process_book(message, http_global)
        if not book:
            return {"error": "not a PDF"}

        # أضفه لـ books.json
        books = await gh_load_books(http_global)
        if not any(b["message_id"] == message_id for b in books):
            books.insert(0, book)
            await gh_save_books(http_global, books, f"add: {book['filename']}")

        return {"success": True, "book": book}
    except Exception as e:
        return {"error": str(e)}

def run_flask():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

# ════════════════════════════════════════
#  Initial scan + listener
# ════════════════════════════════════════

async def initial_scan(http):
    print("🔍 Initial scan...")
    books    = await gh_load_books(http)
    existing = {b["message_id"] for b in books}
    batch    = 0
    count    = 0

    async for message in client.iter_messages(CHANNEL, limit=None):
        if message.id in existing:
            continue
        book = await process_book(message, http)
        if not book:
            continue
        books.insert(0, book)
        batch += 1
        count += 1
        if batch >= BATCH_SIZE:
            await gh_save_books(http, books, f"batch: {count} books")
            print(f"\n💾 Saved — total: {count}\n")
            batch = 0

    if batch > 0:
        await gh_save_books(http, books, f"final: {count} books")

    print(f"\n✅ Initial scan done — {count} new books")

async def main():
    global http_global
    await client.start()

    async with httpx.AsyncClient(timeout=120) as http:
        http_global = http

        # شغّل Flask في thread منفصل
        t = threading.Thread(target=run_flask, daemon=True)
        t.start()
        print("🌐 Flask API running on port 5000")

        await initial_scan(http)

        @client.on(events.NewMessage(chats=CHANNEL))
        async def on_new_book(event):
            print(f"\n🔔 New message!")
            book = await process_book(event.message, http)
            if book:
                books = await gh_load_books(http)
                if not any(b["message_id"] == book["message_id"] for b in books):
                    books.insert(0, book)
                    await gh_save_books(http, books, f"add: {book['filename']}")
                    print(f"  ✅ Added to library")

        print(f"\n👂 Listening in @{CHANNEL}...")
        await client.run_until_disconnected()

asyncio.run(main())
