import os
import re
import uuid
import logging
import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# TensorFlow is optional at import time so the API can still start if ML extras
# have not yet been installed. Install requirements-ml.txt to enable image matching.
try:
    import tensorflow as tf
    TF_AVAILABLE = True
except ImportError:
    tf = None
    TF_AVAILABLE = False

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("findit")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
DATABASE_NAME = os.getenv("DATABASE_NAME", "lost_found_db")

client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = client[DATABASE_NAME]
items_collection = db["items"]
claims_collection = db["claims"]

app = FastAPI(title="FindIt - Intelligent Lost & Found", version="3.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

IMAGE_MODEL = None
IMAGE_MODEL_ERROR = None

# Similarity weights. If a component is unavailable (for example, one report
# has no image), the remaining weights are normalized automatically.
WEIGHTS = {
    "image": 0.45,
    "text": 0.30,
    "category": 0.10,
    "color": 0.08,
    "location": 0.07,
}


@app.on_event("startup")
def create_indexes():
    try:
        client.admin.command("ping")
        items_collection.create_index([("item_type", 1), ("status", 1)])
        items_collection.create_index([("category", 1), ("created_at", DESCENDING)])
        claims_collection.create_index([("item_id", 1), ("created_at", DESCENDING)])
        items_collection.create_index(
            [("title", "text"), ("description", "text"), ("location", "text")]
        )
    except PyMongoError as exc:
        raise RuntimeError(
            "Could not connect to MongoDB. Check MONGODB_URI in .env and make sure MongoDB is running."
        ) from exc


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


def serialize_item(item):
    """Return only public report fields; never expose private keys or contact data."""
    for private_field in ("image_embedding", "resolution_key_hash", "contact_name", "contact_email", "contact_phone"):
        item.pop(private_field, None)
    item["id"] = str(item.pop("_id"))
    return item


# Readable resolution code: 12 random characters (60 bits of entropy).
# Ambiguous characters such as 0/O and 1/I are excluded.
RESOLUTION_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def generate_resolution_key() -> str:
    random_part = "".join(
        secrets.choice(RESOLUTION_CODE_ALPHABET) for _ in range(12)
    )
    return f"FIND-{random_part[:4]}-{random_part[4:8]}-{random_part[8:]}"


def hash_resolution_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_resolution_key(item, key: str) -> bool:
    stored_hash = item.get("resolution_key_hash", "")
    return bool(stored_hash) and secrets.compare_digest(stored_hash, hash_resolution_key(key.strip()))


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def normalized_tokens(value):
    return set(re.findall(r"[a-z0-9]+", clean_text(value).lower()))


def token_jaccard(left, right):
    a, b = normalized_tokens(left), normalized_tokens(right)
    if not a or not b:
        return None
    return len(a & b) / len(a | b)


def item_text(item):
    # Location is scored separately, so do not let it dominate description text.
    parts = [
        item.get("title", ""),
        item.get("category", ""),
        item.get("description", ""),
        item.get("color", ""),
    ]
    return " ".join(parts).lower()


def get_image_model():
    """Load ImageNet-pretrained MobileNetV2 once, without its classification head."""
    global IMAGE_MODEL, IMAGE_MODEL_ERROR
    if not TF_AVAILABLE:
        IMAGE_MODEL_ERROR = "TensorFlow is not installed. Install requirements-ml.txt to enable image similarity."
        return None
    if IMAGE_MODEL is None and IMAGE_MODEL_ERROR is None:
        try:
            IMAGE_MODEL = tf.keras.applications.MobileNetV2(
                weights="imagenet",
                include_top=False,
                pooling="avg",
                input_shape=(224, 224, 3),
            )
            logger.info("ImageNet-pretrained MobileNetV2 loaded for image feature extraction.")
        except Exception as exc:
            IMAGE_MODEL_ERROR = str(exc)
            logger.exception("Could not load MobileNetV2 weights.")
    return IMAGE_MODEL


def extract_image_embedding(image_path: Path):
    """Return a normalized MobileNetV2 feature vector (1280 dimensions)."""
    model = get_image_model()
    if model is None:
        return None
    try:
        with Image.open(image_path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB").resize((224, 224))
            array = np.asarray(image, dtype=np.float32)
        batch = np.expand_dims(array, axis=0)
        batch = tf.keras.applications.mobilenet_v2.preprocess_input(batch)
        features = model.predict(batch, verbose=0)[0].astype(np.float32)
        norm = float(np.linalg.norm(features))
        if norm == 0:
            return None
        return (features / norm).tolist()
    except Exception:
        logger.exception("Image feature extraction failed for %s", image_path)
        return None


def image_similarity(source, candidate):
    source_vector = source.get("image_embedding")
    candidate_vector = candidate.get("image_embedding")
    if not source_vector or not candidate_vector:
        return None
    try:
        a = np.asarray(source_vector, dtype=np.float32)
        b = np.asarray(candidate_vector, dtype=np.float32)
        if a.shape != b.shape or a.size == 0:
            return None
        # Embeddings are L2-normalized, so dot product equals cosine similarity.
        return float(np.clip(np.dot(a, b), 0.0, 1.0))
    except (TypeError, ValueError):
        return None


def text_similarity(source, candidates):
    """Blend word-level and character-level TF-IDF cosine similarity."""
    documents = [item_text(source)] + [item_text(candidate) for candidate in candidates]
    word_scores = np.zeros(len(candidates), dtype=np.float32)
    char_scores = np.zeros(len(candidates), dtype=np.float32)

    try:
        word_matrix = TfidfVectorizer(
            stop_words="english", ngram_range=(1, 2), sublinear_tf=True
        ).fit_transform(documents)
        word_scores = cosine_similarity(word_matrix[0:1], word_matrix[1:]).flatten()
    except ValueError:
        pass

    try:
        char_matrix = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=1
        ).fit_transform(documents)
        char_scores = cosine_similarity(char_matrix[0:1], char_matrix[1:]).flatten()
    except ValueError:
        pass

    return (0.70 * word_scores + 0.30 * char_scores).tolist()


def calculate_match_scores(source, candidates):
    """Combine visual, textual and metadata similarities with missing-data handling."""
    if not candidates:
        return []

    text_scores = text_similarity(source, candidates)
    results = []

    for candidate, text_score in zip(candidates, text_scores):
        components = {"text": float(np.clip(text_score, 0.0, 1.0))}

        visual_score = image_similarity(source, candidate)
        if visual_score is not None:
            components["image"] = visual_score

        source_category = clean_text(source.get("category", "")).lower()
        candidate_category = clean_text(candidate.get("category", "")).lower()
        if source_category and candidate_category:
            components["category"] = 1.0 if source_category == candidate_category else 0.0

        color_score = token_jaccard(source.get("color", ""), candidate.get("color", ""))
        if color_score is not None:
            components["color"] = color_score

        location_score = token_jaccard(source.get("location", ""), candidate.get("location", ""))
        if location_score is not None:
            components["location"] = location_score

        available_weight = sum(WEIGHTS[name] for name in components)
        final_score = sum(components[name] * WEIGHTS[name] for name in components) / available_weight
        details = {f"{name}_similarity": round(value * 100, 1) for name, value in components.items()}
        results.append((candidate, round(final_score * 100, 1), details))

    results.sort(key=lambda row: row[1], reverse=True)
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
    contact_phone: str = Form(""),
    image: Optional[UploadFile] = File(None),
):
    item_type = clean_text(item_type).lower()
    if item_type not in {"lost", "found"}:
        raise HTTPException(status_code=400, detail="Item type must be lost or found.")

    required = {
        "title": title, "category": category, "description": description,
        "location": location, "contact_name": contact_name, "contact_email": contact_email,
    }
    if any(not clean_text(value) for value in required.values()):
        raise HTTPException(status_code=400, detail="Please complete all required fields.")

    image_url = ""
    embedding = None
    warning = None
    if image and image.filename:
        allowed = {".jpg", ".jpeg", ".png", ".webp"}
        extension = Path(image.filename).suffix.lower()
        if extension not in allowed:
            raise HTTPException(status_code=400, detail="Image must be JPG, PNG, or WEBP.")
        content = await image.read()
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image must be 5 MB or smaller.")
        try:
            from io import BytesIO
            with Image.open(BytesIO(content)) as uploaded_image:
                uploaded_image.verify()
        except (UnidentifiedImageError, OSError):
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid image.")

        filename = f"{uuid.uuid4().hex}{extension}"
        image_path = UPLOAD_DIR / filename
        image_path.write_bytes(content)
        image_url = f"/uploads/{filename}"
        embedding = extract_image_embedding(image_path)
        if embedding is None:
            warning = IMAGE_MODEL_ERROR or "Could not extract image features. Text matching will still work."

    resolution_key = generate_resolution_key()
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
        "contact_phone": clean_text(contact_phone),
        "resolution_key_hash": hash_resolution_key(resolution_key),
        "image_url": image_url,
        "status": "active",
        "created_at": datetime.now(timezone.utc),
    }
    if embedding is not None:
        document["image_embedding"] = embedding

    try:
        result = items_collection.insert_one(document)
        document["_id"] = result.inserted_id
        response = {"message": "Item report submitted.", "item": serialize_item(document), "resolution_key": resolution_key}
        if warning:
            response["warning"] = warning
        return response
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


@app.post("/api/items/{item_id}/claims")
def submit_claim(
    item_id: str,
    claimant_name: str = Form(...),
    claimant_email: str = Form(...),
    claimant_phone: str = Form(""),
    message: str = Form(...),
):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(status_code=400, detail="Invalid item ID.")
    item = items_collection.find_one({"_id": ObjectId(item_id)})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")
    if item.get("status") != "active":
        raise HTTPException(status_code=409, detail="This report is already resolved and cannot receive claims.")

    required = [claimant_name, claimant_email, message]
    if any(not clean_text(value) for value in required):
        raise HTTPException(status_code=400, detail="Name, email and claim explanation are required.")

    claim = {
        "item_id": item["_id"],
        "claimant_name": clean_text(claimant_name),
        "claimant_email": clean_text(claimant_email),
        "claimant_phone": clean_text(claimant_phone),
        "message": clean_text(message),
        "created_at": datetime.now(timezone.utc),
    }
    try:
        claims_collection.insert_one(claim)
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not save the ownership claim.") from exc

    # The reporter's contact details are revealed only to a person who submits a claim.
    return {
        "message": "Claim submitted. Contact the reporter outside FindIt to discuss ownership.",
        "reporter_contact": {
            "name": item.get("contact_name", ""),
            "email": item.get("contact_email", ""),
            "phone": item.get("contact_phone", ""),
        },
    }


@app.post("/api/items/{item_id}/manage")
def list_claims(item_id: str, resolution_key: str = Form(...)):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(status_code=400, detail="Invalid item ID.")
    item = items_collection.find_one({"_id": ObjectId(item_id)})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")
    if not verify_resolution_key(item, resolution_key):
        raise HTTPException(status_code=403, detail="Invalid resolution key.")

    claims = list(claims_collection.find({"item_id": item["_id"]}).sort("created_at", DESCENDING))
    output = []
    for claim in claims:
        claim["id"] = str(claim.pop("_id"))
        claim["item_id"] = str(claim["item_id"])
        output.append(claim)
    return {"claims": output, "status": item.get("status", "active")}


@app.post("/api/items/{item_id}/resolve")
def resolve_item(item_id: str, resolution_key: str = Form(...)):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(status_code=400, detail="Invalid item ID.")
    item = items_collection.find_one({"_id": ObjectId(item_id)})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")
    if not verify_resolution_key(item, resolution_key):
        raise HTTPException(status_code=403, detail="Only the original reporter can resolve this report. Check your resolution key.")
    if item.get("status") == "resolved":
        raise HTTPException(status_code=409, detail="This report is already resolved.")
    items_collection.update_one(
        {"_id": item["_id"], "status": "active"},
        {"$set": {"status": "resolved", "resolved_at": datetime.now(timezone.utc)}},
    )
    return {"message": "Report marked as resolved."}


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
    for candidate, score, details in ranked[:5]:
        if score < 5:
            continue
        candidate["match_score"] = score
        candidate["match_details"] = details
        output.append(serialize_item(candidate))

    return {
        "source_item": serialize_item(source),
        "matching_items": output,
        "method": "MobileNetV2 image embeddings + word/character TF-IDF + metadata similarity",
        "note": "The score is a similarity ranking, not a probability that the reports describe the same physical item.",
    }


@app.post("/api/rebuild-image-embeddings")
def rebuild_image_embeddings():
    """Generate embeddings for existing uploaded images that predate this upgrade."""
    if get_image_model() is None:
        raise HTTPException(
            status_code=503,
            detail=IMAGE_MODEL_ERROR or "MobileNetV2 is unavailable. Install requirements-ml.txt.",
        )

    updated = 0
    skipped = 0
    cursor = items_collection.find({
        "image_url": {"$exists": True, "$ne": ""},
        "image_embedding": {"$exists": False},
    })
    for item in cursor:
        filename = Path(item.get("image_url", "")).name
        image_path = UPLOAD_DIR / filename
        if not image_path.is_file():
            skipped += 1
            continue
        embedding = extract_image_embedding(image_path)
        if embedding is None:
            skipped += 1
            continue
        items_collection.update_one(
            {"_id": item["_id"]},
            {"$set": {"image_embedding": embedding}},
        )
        updated += 1
    return {"message": "Image feature rebuild completed.", "updated": updated, "skipped": skipped}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str, resolution_key: str = Form(...)):
    if not ObjectId.is_valid(item_id):
        raise HTTPException(status_code=400, detail="Invalid item ID.")
    item = items_collection.find_one({"_id": ObjectId(item_id)})
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")
    if not verify_resolution_key(item, resolution_key):
        raise HTTPException(status_code=403, detail="Invalid resolution key.")
    items_collection.delete_one({"_id": item["_id"]})
    claims_collection.delete_many({"item_id": item["_id"]})
    if item.get("image_url"):
        image_path = UPLOAD_DIR / Path(item["image_url"]).name
        if image_path.exists():
            image_path.unlink()
    return {"message": "Item report deleted."}


@app.get("/api/stats")
def get_stats():
    pipeline = [{"$group": {
        "_id": "$item_type",
        "total": {"$sum": 1},
        "active": {"$sum": {"$cond": [{"$eq": ["$status", "active"]}, 1, 0]}},
        "resolved": {"$sum": {"$cond": [{"$eq": ["$status", "resolved"]}, 1, 0]}},
    }}]
    rows = list(items_collection.aggregate(pipeline))
    stats = {
        "lost": {"total": 0, "active": 0, "resolved": 0},
        "found": {"total": 0, "active": 0, "resolved": 0},
    }
    for row in rows:
        if row["_id"] in stats:
            stats[row["_id"]] = {
                "total": row["total"], "active": row["active"], "resolved": row["resolved"]
            }
    return stats
