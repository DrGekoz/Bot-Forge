#!/usr/bin/env python3
"""
Bot-Forge Memory Store
Lightweight SQLite-based persistent memory for Discord bots.
Stores facts about users, conversations, and events with tag-based retrieval.
Zero external dependencies — uses only Python stdlib.
"""

import sqlite3
import json
import time
import os
import re
from pathlib import Path
from threading import Lock
from typing import Optional


class MemoryStore:
    """SQLite-backed memory store with tag-based recall.
    
    Each fact has:
      - id (auto-increment)
      - content (the memory text)
      - category (user_pref, project, tool, conversation, general)
      - tags (comma-separated keywords for retrieval)
      - trust_score (0.0 - 1.0, defaults to 0.5)
      - created_at (timestamp)
      - accessed_at (timestamp)
      - access_count (how many times recalled)
    """
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.path.join(os.path.dirname(__file__), "..", "memory_store.db")
        self.db_path = str(Path(db_path).resolve())
        self._lock = Lock()
        self._init_db()
    
    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    tags TEXT NOT NULL DEFAULT '',
                    trust_score REAL NOT NULL DEFAULT 0.5,
                    created_at REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_tags 
                ON memory_facts(tags)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_category
                ON memory_facts(category)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_trust
                ON memory_facts(trust_score)
            """)
            conn.commit()
            conn.close()
    
    def store(self, content: str, category: str = "general", tags: str = "") -> int:
        """Store a memory fact. Returns the fact ID."""
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "INSERT INTO memory_facts (content, category, tags, trust_score, created_at, accessed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (content, category, tags, 0.5, now, now)
            )
            conn.commit()
            fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.close()
            return fact_id
    
    def recall(self, query: str, limit: int = 10, min_trust: float = 0.0) -> list[dict]:
        """Recall facts matching query text or tags.
        
        Searches both content (full-text like) and tags.
        Returns sorted by trust_score descending then access_count descending.
        """
        query_lower = query.lower().strip()
        terms = [t.strip() for t in re.split(r'[\s,;:.!?]+', query_lower) if len(t.strip()) > 2]
        
        if not terms:
            return []
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            
            # Build WHERE clause: match content OR tags for each term
            conditions = []
            params = []
            for term in terms:
                conditions.append("(LOWER(content) LIKE ? OR LOWER(tags) LIKE ?)")
                params.extend([f"%{term}%", f"%{term}%"])
            
            where = " AND ".join(conditions) if conditions else "1=1"
            
            rows = conn.execute(
                f"SELECT * FROM memory_facts WHERE {where} AND trust_score >= ? ORDER BY trust_score DESC, access_count DESC, accessed_at DESC LIMIT ?",
                params + [min_trust, limit]
            ).fetchall()
            
            conn.close()
            
            results = []
            for row in rows:
                results.append({
                    "id": row["id"],
                    "content": row["content"],
                    "category": row["category"],
                    "tags": row["tags"],
                    "trust_score": row["trust_score"],
                    "created_at": row["created_at"],
                    "accessed_at": row["accessed_at"],
                    "access_count": row["access_count"],
                })
            
            # Update accessed_at and access_count for recalled facts
            if results:
                ids = [r["id"] for r in results]
                now = time.time()
                with self._lock:
                    conn2 = sqlite3.connect(self.db_path)
                    for fid in ids:
                        conn2.execute(
                            "UPDATE memory_facts SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                            (now, fid)
                        )
                    conn2.commit()
                    conn2.close()
            
            return results
    
    def recall_by_tags(self, tags: list[str], limit: int = 10) -> list[dict]:
        """Recall facts that have ALL specified tags."""
        tag_conditions = " AND ".join(["LOWER(tags) LIKE ?" for _ in tags])
        params = [f"%{t.lower()}%" for t in tags]
        
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM memory_facts WHERE {tag_conditions} ORDER BY trust_score DESC, accessed_at DESC LIMIT ?",
                params + [limit]
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
    
    def update_trust(self, fact_id: int, delta: float):
        """Adjust a fact's trust score. +0.1 for helpful, -0.1 for unhelpful."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                "UPDATE memory_facts SET trust_score = MIN(1.0, MAX(0.0, trust_score + ?)) WHERE id = ?",
                (delta, fact_id)
            )
            conn.commit()
            conn.close()
    
    def remove(self, fact_id: int):
        """Delete a fact by ID."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,))
            conn.commit()
            conn.close()
    
    def stats(self) -> dict:
        """Get memory store statistics."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            total = conn.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0]
            by_cat = conn.execute(
                "SELECT category, COUNT(*) FROM memory_facts GROUP BY category"
            ).fetchall()
            avg_trust = conn.execute(
                "SELECT AVG(trust_score) FROM memory_facts"
            ).fetchone()[0] or 0
            conn.close()
            return {
                "total_facts": total,
                "by_category": dict(by_cat),
                "average_trust": round(avg_trust, 2),
            }
    
    def format_context(self, results: list[dict], max_lines: int = 8) -> str:
        """Format recalled facts as a prompt context string."""
        if not results:
            return ""
        lines = []
        for r in results[:max_lines]:
            lines.append(f"  - {r['content']} [{r['category']}]")
        return "📚 [Memory]\n" + "\n".join(lines)


# ── Simple module-level singleton for import ──
_store_instance = None


def get_store(db_path: str = None) -> MemoryStore:
    """Get or create the singleton memory store."""
    global _store_instance
    if _store_instance is None:
        _store_instance = MemoryStore(db_path)
    return _store_instance


def recall(query: str, limit: int = 10, min_trust: float = 0.0) -> list[dict]:
    """Convenience function: recall from the singleton store."""
    return get_store().recall(query, limit, min_trust)


def store(content: str, category: str = "general", tags: str = "") -> int:
    """Convenience function: store to the singleton store."""
    return get_store().store(content, category, tags)


if __name__ == "__main__":
    # Quick test
    store = get_store("test_memory.db")
    store.store("User prefers concise responses", "user_pref", "user,preference,communication")
    store.store("Project uses pytest for testing", "project", "project,testing,pytest")
    store.store("Bot personality: Sassy from The Big Lez Show", "general", "bot,sassy,personality")
    
    print("Stored 3 test facts")
    print(f"Stats: {store.stats()}")
    
    results = store.recall("testing")
    print(f"\nRecalled 'testing': {results}")
    
    results = store.recall("sassy")
    print(f"\nRecalled 'sassy': {results}")
    
    print(f"\nFormatted context:\n{store.format_context(results)}")
    
    # Clean up
    os.remove("test_memory.db")
    print("\n✅ Memory store works!")
