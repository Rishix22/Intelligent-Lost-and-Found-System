import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "lost_found_db")

client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = client[DATABASE_NAME]
items_collection = db["items"]

app = FastAPI(title="Lost & Found", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


@app.on_event("startup")
def create_indexes():
    try:
        client.admin.command("ping")
        items_collection.create_index([("item_type", 1), ("status", 1)])
        items_collection.create_index([("category", 1), ("created_at", DESCENDING)])
        items_collection.create_index([("title", "text"), ("description", "text"), ("location", "text")])
    except PyMongoError as exc:
        raise RuntimeError(
            "Could not connect to MongoDB. Check MONGODB_URI in your .env file "
            "and make sure MongoDB is running."
        ) from exc


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


def serialize_item(item):
    item["id"] = str(item.pop("_id"))
    return item


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def item_text(item):
    parts = [
        item.get("title", ""),
        item.get("category", ""),
        item.get("description", ""),
        item.get("color", ""),
        item.get("location", ""),
    ]
    return " ".join(parts).lower()


def calculate_match_scores(source, candidates):
    """Lightweight, training-free text similarity using TF-IDF and cosine similarity."""
    if not candidates:
        return []

    documents = [item_text(source)] + [item_text(candidate) for candidate in candidates]
    try:
        matrix = TfidfVectorizer(
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True
        ).fit_transform(documents)
        text_scores = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
    except ValueError:
        text_scores = [0.0] * len(candidates)

    results = []
    for candidate, text_score in zip(candidates, text_scores):
        # Small metadata boosts help when descriptions are short.
        category_bonus = 0.15 if source.get("category") == candidate.get("category") else 0.0
        source_color = clean_text(source.get("color", "")).lower()
        candidate_color = clean_text(candidate.get("color", "")).lower()
        color_bonus = 0.10 if source_color and source_color == candidate_color else 0.0
        score = min(1.0, float(text_score) * 0.75 + category_bonus + color_bonus)
        results.append((candidate, round(score * 100, 1)))

    results.sort(key=lambda pair: pair[1], reverse=True)
    return results


@app.post("/api/items")
async def create_item(
    item_type: str = Form(...),
    title: str = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    color: str = Form(""),
    location: str = Form(...),
    event_date: str = Form(""),
    contact_name: str = Form(...),
    contact_email: str = Form(...),
    image: Optional[UploadFile] = File(None),
):
    item_type = clean_text(item_type).lower()
    if item_type not in {"lost", "found"}:
        raise HTTPException(status_code=400, detail="Item type must be lost or found.")

    required = {
        "title": title,
        "category": category,
        "description": description,
        "location": location,
        "contact_name": contact_name,
        "contact_email": contact_email,
    }
    if any(not clean_text(value) for value in required.values()):
        raise HTTPException(status_code=400, detail="Please complete all required fields.")

    image_url = ""
    if image and image.filename:
        allowed = {".jpg", ".jpeg", ".png", ".webp"}
        extension = Path(image.filename).suffix.lower()
        if extension not in allowed:
            raise HTTPException(status_code=400, detail="Image must be JPG, PNG, or WEBP.")
        content = await image.read()
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image must be 5 MB or smaller.")
        filename = f"{uuid.uuid4().hex}{extension}"
        (UPLOAD_DIR / filename).write_bytes(content)
        image_url = f"/uploads/{filename}"

    document = {
        "item_type": item_type,
        "title": clean_text(title),
        "category": clean_text(category),
        "description": clean_text(description),
        "color": clean_text(color),
        "location": clean_text(location),
        "event_date": clean_text(event_date),
        "contact_name": clean_text(contact_name),
        "contact_email": clean_text(contact_email),
        "image_url": image_url,
        "status": "active",
        "created_at": datetime.now(timezone.utc),
    }

    try:
        result = items_collection.insert_one(document)
        document["_id"] = result.inserted_id
        return {"message": "Item report submitted.", "item": serialize_item(document)}
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not save the item to MongoDB.") from exc


@app.get("/api/items")
def list_items(
    item_type: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    status: str = "active",
):
    query = {}
    if item_type in {"lost", "found"}:
        query["item_type"] = item_type
    if category:
        query["category"] = category
    if status in {"active", "resolved"}:
        query["status"] = status
    if q and clean_text(q):
        query["$text"] = {"$search": clean_text(q)}

    try:
        cursor = items_collection.find(query).sort("created_at", DESCENDING).limit(100)
        return [serialize_item(item) for item in cursor]
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not retrieve items.") from exc


@app.get("/api/items/{item_id}/matches")
def get_matches(item_id: str):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(status_code=400, detail="Invalid item ID.")

    source = items_collection.find_one({"_id": ObjectId(item_id)})
    if not source:
        raise HTTPException(status_code=404, detail="Item not found.")

    opposite_type = "found" if source["item_type"] == "lost" else "lost"
    candidates = list(items_collection.find({
        "item_type": opposite_type,
        "status": "active",
        "_id": {"$ne": source["_id"]},
    }).limit(300))

    ranked = calculate_match_scores(source, candidates)
    output = []
    for candidate, score in ranked[:5]:
        if score < 5:
            continue
        candidate["match_score"] = score
        output.append(serialize_item(candidate))

    return {
        "source_item": serialize_item(source),
        "matching_items": output,
        "method": "TF-IDF text similarity + category and color matching",
        "note": "Match score is a similarity ranking, not a probability that the items are identical."
    }


@app.patch("/api/items/{item_id}/status")
def update_status(item_id: str, status: str):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(status_code=400, detail="Invalid item ID.")
    if status not in {"active", "resolved"}:
        raise HTTPException(status_code=400, detail="Status must be active or resolved.")

    result = items_collection.update_one(
        {"_id": ObjectId(item_id)},
        {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Item not found.")
    return {"message": f"Item marked as {status}."}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(status_code=400, detail="Invalid item ID.")
    item = items_collection.find_one_and_delete({"_id": ObjectId(item_id)})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")

    if item.get("image_url"):
        filename = Path(item["image_url"]).name
        image_path = UPLOAD_DIR / filename
        if image_path.exists():
            image_path.unlink()

    return {"message": "Item report deleted."}


@app.get("/api/stats")
def get_stats():
    pipeline = [
        {"$group": {
            "_id": "$item_type",
            "total": {"$sum": 1},
            "active": {"$sum": {"$cond": [{"$eq": ["$status", "active"]}, 1, 0]}},
            "resolved": {"$sum": {"$cond": [{"$eq": ["$status", "resolved"]}, 1, 0]}},
        }}
    ]
    rows = list(items_collection.aggregate(pipeline))
    stats = {
        "lost": {"total": 0, "active": 0, "resolved": 0},
        "found": {"total": 0, "active": 0, "resolved": 0},
    }
    for row in rows:
        if row["_id"] in stats:
            stats[row["_id"]] = {
                "total": row["total"],
                "active": row["active"],
                "resolved": row["resolved"],
            }
    return stats
