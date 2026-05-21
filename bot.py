import asyncio
import json
import base64
import os
import httpx
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

# ── من Heroku Config Vars ──
API_ID      = int(os.environ["TG_API_ID"])
API_HASH    = os.environ["TG_API_HASH"]
CHANNEL     = os.environ["TG_CHANNEL"]        # مثال: Sportive_Books
GH_TOKEN    = os.environ["GH_TOKEN"]          # GitHub Personal Access Token
GH_REPO     = os.environ["GH_REPO"]           # مثال: CreeD/sportive-books
GH_BRANCH   = os.environ.get("GH_BRANCH", "main")

BOOKS_PATH  = "data/books.json"
THUMBS_DIR  = "data/thumbs"

client = TelegramClient("session", API_ID, API_HASH)

# ── GitHub API ──
async def gh_get_sha(http, path):
    """جيب SHA الملف الحالي عشان نعمل update"""
    r = await http.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{path}",
        headers={"Authorization": f"token {GH_TOKEN}"},
        params={"ref": GH_BRANCH}
    )
    if r.status_code == 200:
        return r.json().get("sha")
    return None

async def gh_upload(http, path, content_bytes, message="update"):
    """ارفع أو حدّث ملف على GitHub"""
    sha = await gh_get_sha(http, path)
    body = {
        "message": message,
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

async def load_books(http):
    """حمّل books.json من GitHub"""
    r = await http.get(
        f"https://api.github.com/repos/{GH_REPO}/contents/{BOOKS_PATH}",
        headers={"Authorization": f"token {GH_TOKEN}"},
        params={"ref": GH_BRANCH}
    )
    if r.status_code == 200:
        content = base64.b64decode(r.json()["content"]).decode("utf-8")
        return json.loads(content)
    return []

# ── معالجة الكتاب ──
async def process_message(message, http):
    if not (message.media and isinstance(message.media, MessageMediaDocument)):
        return

    doc = message.media.document
    filename = None
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
            break

    if not (filename and filename.lower().endswith(".pdf")):
        return

    size_mb = round(doc.size / 1_048_576, 2)
    print(f"📥 [{size_mb} MB] {filename}")

    # ── حمّل الـ thumbnail بس (مش الـ PDF) ──
    thumb_url = None
    if doc.thumbs:
        try:
            thumb_bytes = await client.download_media(message, file=bytes, thumb=0)
            if thumb_bytes:
                safe_name  = f"{message.id}.jpg"
                thumb_path = f"{THUMBS_DIR}/{safe_name}"
                ok = await gh_upload(http, thumb_path, thumb_bytes, f"thumb: {filename}")
                if ok:
                    thumb_url = f"https://raw.githubusercontent.com/{GH_REPO}/{GH_BRANCH}/{thumb_path}"
                    print(f"  🖼️ thumbnail uploaded")
        except Exception as e:
            print(f"  ⚠️ thumb error: {e}")

    # ── حمّل الـ PDF وارفعه ──
    pdf_url = None
    try:
        pdf_bytes = await client.download_media(message, file=bytes)
        if pdf_bytes:
            safe_filename = filename.replace(" ", "_")
            pdf_path      = f"data/books/{safe_filename}"
            ok = await gh_upload(http, pdf_path, pdf_bytes, f"book: {filename}")
            if ok:
                pdf_url = f"https://raw.githubusercontent.com/{GH_REPO}/{GH_BRANCH}/{pdf_path}"
                print(f"  📚 PDF uploaded")
    except Exception as e:
        print(f"  ⚠️ PDF error: {e}")

    # ── حدّث books.json ──
    books = await load_books(http)

    # تأكد مش موجود قبل كده
    if any(b["message_id"] == message.id for b in books):
        return

    books.insert(0, {
        "title"      : filename.replace(".pdf", ""),
        "filename"   : filename,
        "size_mb"    : size_mb,
        "date"       : message.date.strftime("%Y-%m-%d"),
        "caption"    : (message.message or "").strip(),
        "link"       : f"https://t.me/{CHANNEL}/{message.id}",
        "message_id" : message.id,
        "thumb_url"  : thumb_url,
        "pdf_url"    : pdf_url
    })

    json_bytes = json.dumps(books, ensure_ascii=False, indent=2).encode("utf-8")
    await gh_upload(http, BOOKS_PATH, json_bytes, f"add: {filename}")
    print(f"  ✅ books.json updated — total: {len(books)}")

# ── Scan تاريخي عند البداية ──
async def initial_scan(http):
    print("🔍 Initial scan...")
    books = await load_books(http)
    existing_ids = {b["message_id"] for b in books}

    async for message in client.iter_messages(CHANNEL, limit=None):
        if message.id not in existing_ids:
            await process_message(message, http)

    print("✅ Initial scan done")

# ── استقبال الرسائل الجديدة ──
async def main():
    await client.start()
    async with httpx.AsyncClient(timeout=60) as http:
        await initial_scan(http)

        @client.on(events.NewMessage(chats=CHANNEL))
        async def handler(event):
            await process_message(event.message, http)

        print("👂 Listening for new books...")
        await client.run_until_disconnected()

asyncio.run(main())
