import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import csv
import io

from database import db, create_document, get_documents
from bson import ObjectId

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Allowed clubs list for Maltese Youth League enforcement
ALLOWED_CLUBS = [
    "HIBS",
    "BIRKIRKARA",
    "BALZAN",
    "ZABBAR",
    "NAXXAR",
    "QORMI",
    "MARSASKALA",
    "KIRKOP",
    "GOZO",
    "MELLIEHA",
]
ALLOWED_CLUBS_SET = set(ALLOWED_CLUBS)

# Helpers

def oid(id_str: str) -> ObjectId:
    try:
        return ObjectId(id_str)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")


def serialize(doc):
    if not doc:
        return doc
    doc["id"] = str(doc.pop("_id"))
    # Convert datetime to isoformat
    for k, v in list(doc.items()):
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


# Models for request bodies
class LeagueIn(BaseModel):
    name: str
    season: Optional[str] = None

class TeamIn(BaseModel):
    name: str
    short_name: Optional[str] = None

class MatchUpdate(BaseModel):
    home_score: Optional[int] = None
    away_score: Optional[int] = None
    status: Optional[str] = None  # scheduled | played | postponed | cancelled


@app.get("/")
def read_root():
    return {"message": "Football Information API running"}


# Admin: create league
@app.post("/api/leagues")
def create_league(payload: LeagueIn):
    league_id = create_document("league", {
        "name": payload.name,
        "season": payload.season,
        "country": "Malta"
    })
    return {"id": league_id, "name": payload.name, "season": payload.season}

# List leagues
@app.get("/api/leagues")
def list_leagues():
    leagues = [serialize(d) for d in db["league"].find().sort("created_at", -1)]
    return leagues

# Admin: add teams to a league
@app.post("/api/leagues/{league_id}/teams")
def add_team(league_id: str, payload: TeamIn):
    if not db["league"].find_one({"_id": oid(league_id)}):
        raise HTTPException(status_code=404, detail="League not found")
    team_id = create_document("team", {
        "league_id": league_id,
        "name": payload.name,
        "short_name": payload.short_name
    })
    return {"id": team_id}

# Get teams by league
@app.get("/api/leagues/{league_id}/teams")
def get_teams(league_id: str):
    teams = [serialize(d) for d in db["team"].find({"league_id": league_id}).sort("name", 1)]
    return teams

# Admin: upload fixtures CSV (home_team,away_team,match_date,venue)
@app.post("/api/leagues/{league_id}/fixtures/upload")
async def upload_fixtures(league_id: str, file: UploadFile = File(...)):
    if not db["league"].find_one({"_id": oid(league_id)}):
        raise HTTPException(status_code=404, detail="League not found")

    content = await file.read()
    try:
        text = content.decode("utf-8")
    except Exception:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))

    # Build mapping team name -> id
    teams = {t["name"].strip().lower(): t["_id"] for t in db["team"].find({"league_id": league_id})}

    inserted = 0
    for row in reader:
        home = row.get("home_team", "").strip().lower()
        away = row.get("away_team", "").strip().lower()
        match_date = row.get("match_date") or None
        venue = row.get("venue") or None
        if home not in teams or away not in teams:
            # skip unknown teams
            continue
        create_document("match", {
            "league_id": league_id,
            "home_team_id": str(teams[home]),
            "away_team_id": str(teams[away]),
            "match_date": match_date,
            "venue": venue,
            "status": "scheduled"
        })
        inserted += 1

    return {"inserted": inserted}

# Admin: update match score/status
@app.patch("/api/matches/{match_id}")
def update_match(match_id: str, payload: MatchUpdate):
    m = db["match"].find_one({"_id": oid(match_id)})
    if not m:
        raise HTTPException(status_code=404, detail="Match not found")
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if updates:
        updates["updated_at"] = datetime.now(timezone.utc)
        db["match"].update_one({"_id": m["_id"]}, {"$set": updates})
    return {"id": match_id, **updates}

# Public: list standings (computed on the fly from matches and teams)
@app.get("/api/leagues/{league_id}/standings")
def get_standings(league_id: str):
    # Initialize table
    teams = list(db["team"].find({"league_id": league_id}))
    if not teams:
        return []

    table = {
        str(t["_id"]): {
            "team_id": str(t["_id"]),
            "team_name": t["name"],
            "P": 0, "W": 0, "D": 0, "L": 0, "F": 0, "A": 0, "GD": 0, "Pts": 0
        } for t in teams
    }

    # Accumulate from played matches
    for m in db["match"].find({"league_id": league_id, "status": "played"}):
        h = str(m.get("home_team_id"))
        a = str(m.get("away_team_id"))
        hs = int(m.get("home_score", 0) or 0)
        as_ = int(m.get("away_score", 0) or 0)
        table[h]["P"] += 1
        table[a]["P"] += 1
        table[h]["F"] += hs
        table[h]["A"] += as_
        table[a]["F"] += as_
        table[a]["A"] += hs
        if hs > as_:
            table[h]["W"] += 1
            table[a]["L"] += 1
            table[h]["Pts"] += 3
        elif hs < as_:
            table[a]["W"] += 1
            table[h]["L"] += 1
            table[a]["Pts"] += 3
        else:
            table[h]["D"] += 1
            table[a]["D"] += 1
            table[h]["Pts"] += 1
            table[a]["Pts"] += 1

    # Compute GD and sorting with tie-breakers: head-to-head then goal difference then goals for then name
    for row in table.values():
        row["GD"] = row["F"] - row["A"]

    # Simple tie-breakers (no head-to-head cache available without extra queries) -> GD, F, name
    rows = list(table.values())
    rows.sort(key=lambda r: (-r["Pts"], -r["GD"], -r["F"], r["team_name"]))
    for i, r in enumerate(rows, start=1):
        r["position"] = i
    return rows

# Public: upcoming matches
@app.get("/api/leagues/{league_id}/matches/upcoming")
def upcoming_matches(league_id: str, limit: int = 20):
    cur = db["match"].find({"league_id": league_id, "status": {"$in": ["scheduled", "postponed"]}}).sort("match_date", 1).limit(limit)
    items = []
    team_map = {str(t["_id"]): t["name"] for t in db["team"].find({"league_id": league_id})}
    for m in cur:
        items.append({
            "id": str(m["_id"]),
            "home": team_map.get(str(m.get("home_team_id")), ""),
            "away": team_map.get(str(m.get("away_team_id")), ""),
            "date": m.get("match_date"),
            "venue": m.get("venue"),
            "status": m.get("status")
        })
    return items

# Public: recent results
@app.get("/api/leagues/{league_id}/matches/results")
def recent_results(league_id: str, limit: int = 20):
    cur = db["match"].find({"league_id": league_id, "status": "played"}).sort("match_date", -1).limit(limit)
    items = []
    team_map = {str(t["_id"]): t["name"] for t in db["team"].find({"league_id": league_id})}
    for m in cur:
        items.append({
            "id": str(m["_id"]),
            "home": team_map.get(str(m.get("home_team_id")), ""),
            "away": team_map.get(str(m.get("away_team_id")), ""),
            "score": f"{m.get('home_score', 0)} - {m.get('away_score', 0)}",
            "date": m.get("match_date")
        })
    return items

# Public: teams info
@app.get("/api/leagues/{league_id}/teams/{team_id}")
def team_info(league_id: str, team_id: str):
    t = db["team"].find_one({"_id": oid(team_id), "league_id": league_id})
    if not t:
        raise HTTPException(status_code=404, detail="Team not found")
    # Team fixtures
    upcoming = list(db["match"].find({"league_id": league_id, "status": {"$in": ["scheduled", "postponed"]}, "$or": [
        {"home_team_id": team_id}, {"away_team_id": team_id}
    ]}).sort("match_date", 1).limit(10))
    results = list(db["match"].find({"league_id": league_id, "status": "played", "$or": [
        {"home_team_id": team_id}, {"away_team_id": team_id}
    ]}).sort("match_date", -1).limit(10))
    def map_match(m):
        return {
            "id": str(m["_id"]),
            "home_team_id": str(m.get("home_team_id")),
            "away_team_id": str(m.get("away_team_id")),
            "home_score": m.get("home_score"),
            "away_score": m.get("away_score"),
            "status": m.get("status"),
            "date": m.get("match_date")
        }
    return {
        "id": str(t["_id"]),
        "name": t["name"],
        "short_name": t.get("short_name"),
        "upcoming": [map_match(m) for m in upcoming],
        "results": [map_match(m) for m in results]
    }

# Admin: enforce the exact clubs list for a league (delete extras, add missing)
@app.post("/api/leagues/{league_id}/enforce-teams")
def enforce_teams(league_id: str):
    if not db["league"].find_one({"_id": oid(league_id)}):
        raise HTTPException(status_code=404, detail="League not found")

    # Delete teams not in allowed list
    to_delete = list(db["team"].find({
        "league_id": league_id,
        "name": {"$nin": list(ALLOWED_CLUBS_SET)}
    }, {"_id": 1}))
    deleted_count = 0
    if to_delete:
        ids = [t["_id"] for t in to_delete]
        res = db["team"].delete_many({"_id": {"$in": ids}})
        deleted_count = res.deleted_count

    # Ensure all allowed teams exist
    created_count = 0
    for name in ALLOWED_CLUBS:
        if not db["team"].find_one({"league_id": league_id, "name": name}):
            create_document("team", {"league_id": league_id, "name": name, "short_name": name})
            created_count += 1

    return {
        "deleted_non_allowed": deleted_count,
        "created_missing": created_count,
        "allowed": ALLOWED_CLUBS,
    }

# Seed example league with provided teams (enforces exact set)
@app.post("/api/seed/maltese-youth-league")
def seed_maltese_league():
    name = "Maltese Youth League"
    existing = db["league"].find_one({"name": name})
    if existing:
        league_id = str(existing["_id"])
    else:
        league_id = create_document("league", {"name": name, "season": None, "country": "Malta"})

    # Remove any teams not in the allowed list and ensure allowed list is present
    _ = enforce_teams(league_id)

    return {"league_id": league_id, "teams": ALLOWED_CLUBS}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    response["database_url"] = "✅ Set" if os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
