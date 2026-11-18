"""Fail-closed intersection of the frozen owner theme shortlist and the live grant.

Editorial topic scope can only REMOVE authorized complete passages. It cannot
authorize a verse, change source evidence, split a coherent passage, or advance
a publication cycle. The production RPC stays the authorization authority.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from engine.quran import STANDARD_SURAH_AYAH_COUNTS
from engine.production_authorization import (
    ProductionAuthorizationCatalog,
    ProductionAuthorizationError,
)

CATALOG_FILE=Path(__file__).resolve().parents[1]/"data/editorial/calm_reels_shortlist_v1.json"
CATALOG_SHA256="df3eea4aefd469c838e3c9c31c58603122b8c8eaab50f0d36ef10d90a181666d"
CATALOG_ID="calm-reels-editorial-shortlist-v1"
CANONICAL_KEY=re.compile(r"[1-9][0-9]*:[1-9][0-9]*\Z")
SHA256=re.compile(r"[a-f0-9]{64}\Z")

class CalmReelsScopeError(ValueError):
    """Owner's immutable nonpublishing topic scope is missing or incompatible."""

@lru_cache(maxsize=1)
def owner_shortlist_keys()->frozenset[str]:
    try:
        raw=CATALOG_FILE.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=CATALOG_SHA256:
            raise ValueError("Frozen owner list byte hash mismatch")
        d=json.loads(raw)
        if (type(d.get("schema_version")) is not int or d["schema_version"]!=1
            or d.get("catalog_id")!=CATALOG_ID
            or d.get("status")!="FROZEN_OWNER_EDITORIAL_SCOPE_ONLY"
            or d.get("publication_authorized") is not False
            or d.get("independent_context_review_required") is not True
            or type(d.get("ayah_count")) is not int or d["ayah_count"]!=2211
            or type(d.get("ayahs")) is not list or len(d["ayahs"])!=2211):
            raise ValueError("Invalid owner editorial manifest")
        keys=[]
        for entry in d["ayahs"]:
            if not isinstance(entry,dict) or set(entry)!={"key","arabic_sha256"}:
                raise ValueError("Unexpected owner editorial member")
            key=entry["key"]
            if (not isinstance(key,str) or not CANONICAL_KEY.fullmatch(key)
                or not isinstance(entry["arabic_sha256"],str)
                or not SHA256.fullmatch(entry["arabic_sha256"])):
                raise ValueError("Invalid source-locked owner editorial member")
            surah,ayah=(int(n) for n in key.split(":"))
            if surah not in STANDARD_SURAH_AYAH_COUNTS or not 1<=ayah<=STANDARD_SURAH_AYAH_COUNTS[surah]:
                raise ValueError("Owner catalogue contains an invalid verse")
            keys.append(key)
        if len(set(keys))!=2211:
            raise ValueError("Owner catalogue contains duplicate verses")
        return frozenset(keys)
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise CalmReelsScopeError("CALM_REELS_FROZEN_SCOPE_UNAVAILABLE") from error

def restrict_to_owner_shortlist(
    authorized:ProductionAuthorizationCatalog,
)->ProductionAuthorizationCatalog:
    """Only exact existing grants all of whose members belong to the owner list."""
    if not isinstance(authorized,ProductionAuthorizationCatalog):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_RESPONSE_INVALID")
    if authorized.policy_version!="calm-inviting-v1" or not authorized.passages:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_POLICY_MISMATCH")
    owner=owner_shortlist_keys()
    retained=tuple(p for p in authorized.passages if p.verse_keys.issubset(owner))
    if not retained:
        raise ProductionAuthorizationError("CALM_REELS_NO_FULLY_AUTHORIZED_PASSAGES")
    return ProductionAuthorizationCatalog(
        policy_version=authorized.policy_version,
        passages=retained,
    )
