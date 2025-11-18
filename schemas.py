"""
Database Schemas for Football Information App

Each Pydantic model below corresponds to a MongoDB collection.
Collection name is the lowercase class name (e.g., League -> "league").
"""
from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List

class League(BaseModel):
    name: str = Field(..., description="League name")
    season: Optional[str] = Field(None, description="Season label, e.g., 2024/25")
    country: Optional[str] = Field("Malta", description="Country of the league")

class Team(BaseModel):
    league_id: str = Field(..., description="League ID (stringified ObjectId)")
    name: str = Field(..., description="Team name")
    short_name: Optional[str] = Field(None, description="Abbreviation or short name")

class Match(BaseModel):
    league_id: str = Field(..., description="League ID (stringified ObjectId)")
    home_team_id: str = Field(..., description="Home team ID")
    away_team_id: str = Field(..., description="Away team ID")
    match_date: Optional[str] = Field(None, description="ISO date string (UTC)")
    venue: Optional[str] = Field(None, description="Stadium/venue")
    status: str = Field("scheduled", description="scheduled | played | postponed | cancelled")
    home_score: Optional[int] = Field(None, ge=0)
    away_score: Optional[int] = Field(None, ge=0)

class StandingRow(BaseModel):
    team_id: str
    team_name: str
    P: int
    W: int
    D: int
    L: int
    F: int
    A: int
    GD: int
    Pts: int
    position: Optional[int] = None

class Standing(BaseModel):
    league_id: str
    rows: List[StandingRow]
    last_updated: Optional[str] = None

class AppUser(BaseModel):
    name: str
    email: str
    league_ids: Optional[List[str]] = Field(default_factory=list, description="Leagues this user can view")
    role: str = Field("user", description="user | admin")
    is_active: bool = True
