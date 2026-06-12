from pydantic import BaseModel
from typing import Optional


class SnapshotRow(BaseModel):
    id: str
    grain: str
    deliveryMonth: str
    futuresSymbol: str
    basisCents: Optional[int]
    isSpot: bool = False
    spotGrain: Optional[str] = None  # when isSpot=True, which grain this spot represents


class Snapshot(BaseModel):
    id: Optional[int] = None
    timestamp: str
    provider: str   # "ADM" or "Mendota"
    location: str
    source: str = "manual"  # "manual" or "email"
    emailSubject: Optional[str] = None
    emailDate: Optional[str] = None
    rows: list[SnapshotRow] = []


class NewSnapshotRequest(BaseModel):
    timestamp: str
    provider: str
    location: str
    source: str = "manual"
    emailSubject: Optional[str] = None
    emailDate: Optional[str] = None
    rows: list[SnapshotRow]


class EmailMessage(BaseModel):
    id: str
    subject: str
    receivedAt: str
    sender: str
    hasAttachment: bool
    body: Optional[str] = None


class ParsedSnapshot(BaseModel):
    emailId: str
    emailSubject: str
    emailDate: str
    provider: str
    snapshots: list[NewSnapshotRequest]
    parseError: Optional[str] = None
    needsReview: bool = False
