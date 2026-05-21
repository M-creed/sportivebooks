import asyncio
import json
import base64
import os
import httpx
import fitz
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
BATCH_SIZE = 10  # احفظ books.json كل 10 كتب

client = TelegramClient("session", API_ID, API_HASH)

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
#  معالجة كتاب واحد — بدون حفظ books.json
# ════════════════════════════════════════

async def process_book(message, http):
    """بيرجع dict الكتاب أو None — بدون ما يلمس books.json"""
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

    # ── 1. حمّل الـ PDF ──
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

    # ── 2. thumbnail من أول صفحة ──
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
#  Initial scan — batch saving
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

        # احفظ books.json كل BATCH_SIZE كتب بس
        if batch >= BATCH_SIZE:
            await gh_save_books(http, books, f"batch: {count} books")
            print(f"\n💾 Saved — total so far: {count}\n")
            batch = 0

    # احفظ الباقي
    if batch > 0:
        await gh_save_books(http, books, f"final: {count} books")

    print(f"\n✅ Initial scan done — {count} new books")

# ════════════════════════════════════════
#  New message — حفظ فوري
# ════════════════════════════════════════

async def process_message(message, http):
    """للرسائل الجديدة — بيحفظ books.json فوراً"""
    books = await gh_load_books(http)

    if any(b["message_id"] == message.id for b in books):
        return

    book = await process_book(message, http)
    if not book:
        return

    books.insert(0, book)
    await gh_save_books(http, books, f"add: {book['filename']}")
    print(f"  ✅ books.json updated — total: {len(books)}")

# ════════════════════════════════════════
#  Main
# ════════════════════════════════════════

async def main():
    await client.start()
    async with httpx.AsyncClient(timeout=120) as http:
        await initial_scan(http)

        @client.on(events.NewMessage(chats=CHANNEL))
        async def on_new_book(event):
            print(f"\n🔔 New message!")
            await process_message(event.message, http)

        print(f"\n👂 Listening for new books in @{CHANNEL}...")
        await client.run_until_disconnected()

asyncio.run(main())
