# FindIt — Lost & Found Management System

A small college project built with **FastAPI, PyMongo, MongoDB, HTML, CSS, JavaScript, and scikit-learn**.

## Features

- Submit lost and found reports, including optional images.
- Browse, filter, and search active reports.
- Compare a report against reports of the opposite type.
- Rank possible matches using TF-IDF and cosine similarity.
- Mark reports as resolved or delete them.
- Dashboard statistics generated with a MongoDB aggregation pipeline.
- MongoDB indexes for common filters and text search.

## ML approach

This project uses `TfidfVectorizer` and cosine similarity. It does not train a supervised model and does not require a labeled training dataset. It creates text vectors from the current item and candidate reports, then ranks candidates by similarity. Matching category and color add small score boosts.

The displayed score is a **similarity score, not a probability** that two reports describe the same physical object. A person must verify ownership.

## Requirements

- Python 3.10+
- MongoDB Community Server running locally, or a MongoDB Atlas cluster
- Internet connection for the optional Google Fonts used by the frontend

## Setup

1. Extract the ZIP.
2. Open the extracted folder in VS Code.
3. Create and activate a virtual environment (recommended).
4. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

5. Copy `.env.example` to `.env`.
6. Set `MONGODB_URI` in `.env`:
   - Local: `mongodb://localhost:27017`
   - Atlas: paste your Atlas connection string and replace the username/password.
7. Start the application:

   ```bash
   uvicorn main:app --reload
   ```

8. Open `http://127.0.0.1:8000`.

MongoDB database and collection are created automatically when the first report is inserted. Indexes are created when the application starts.

## Main API routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/items` | List and filter reports |
| POST | `/api/items` | Create a lost/found report |
| GET | `/api/items/{id}/matches` | Find potential matches |
| PATCH | `/api/items/{id}/status?status=resolved` | Update report status |
| DELETE | `/api/items/{id}` | Delete a report |
| GET | `/api/stats` | Dashboard statistics |
| GET | `/docs` | Interactive FastAPI API documentation |

## MongoDB design

Database: `lost_found_db`

Collection: `items`

Each document stores the report type, title, category, description, color, location, event date, contact details, image path, status, and creation timestamp.

The project intentionally uses one main collection to keep the college implementation small. It still demonstrates BSON documents, CRUD, text and compound indexes, schema flexibility, and aggregation.

## Notes and limitations

- Uploaded images are saved in the local `uploads/` directory; MongoDB stores their paths. This keeps the database documents small. GridFS can be added if storing files inside MongoDB is a course requirement.
- This is a classroom prototype, not a production deployment. It does not include user authentication, authorization, email verification, or privacy controls. Add those before deploying for public use.
- Contact details are displayed in report cards for demonstration. Use test data in a college demo.
- The match algorithm compares text and metadata; it does not compare image pixels.
- The frontend uses same-origin API calls, so no separate CORS configuration is needed.
