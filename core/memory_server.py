#!/usr/bin/env python3
"""
Bot-Forge Memory API Server
Lightweight HTTP server for the memory store.
Bots can recall/store memories via HTTP POST/GET.
"""

import json
import os
import sys
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Add parent to path so we can import memory_store
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.memory_store import MemoryStore


_store = None


class MemoryHandler(BaseHTTPRequestHandler):
    
    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length > 0:
            raw = self.rfile.read(length)
            return json.loads(raw.decode())
        return {}
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    
    def do_GET(self):
        global _store
        path = self.path.rstrip("/")
        
        if path == "/stats":
            self._send_json(_store.stats())
        
        elif path.startswith("/recall/"):
            query = path[len("/recall/"):]
            query = query.split("?")[0]  # strip any query params
            from urllib.parse import unquote
            query = unquote(query)
            limit = int(self.path.split("limit=")[1]) if "limit=" in self.path else 10
            results = _store.recall(query, limit=limit)
            self._send_json({"results": results, "count": len(results)})
        
        elif path == "/":
            self._send_json({
                "status": "ok",
                "store": "Bot-Forge Memory",
                "endpoints": {
                    "GET /stats": "Memory store statistics",
                    "GET /recall/<query>": "Search memories by text",
                    "POST /store": "Store a new memory",
                    "POST /feedback/<id>": "Rate a fact's helpfulness",
                    "GET /tags/<tag1,tag2>": "Recall facts by tags",
                }
            })
        
        else:
            self._send_json({"error": "Not found"}, 404)
    
    def do_POST(self):
        global _store
        path = self.path.rstrip("/")
        body = self._read_body()
        
        if path == "/store":
            content = body.get("content", "")
            category = body.get("category", "general")
            tags = body.get("tags", "")
            if not content:
                self._send_json({"error": "content is required"}, 400)
                return
            fact_id = _store.store(content, category, tags)
            self._send_json({"id": fact_id, "status": "stored"})
        
        elif path.startswith("/feedback/"):
            try:
                fact_id = int(path[len("/feedback/"):].split("/")[0])
                helpful = body.get("helpful", True)
                delta = 0.1 if helpful else -0.1
                _store.update_trust(fact_id, delta)
                self._send_json({"status": "updated", "fact_id": fact_id, "delta": delta})
            except (ValueError, IndexError):
                self._send_json({"error": "Invalid fact ID"}, 400)
        
        else:
            self._send_json({"error": "Not found"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Bot-Forge Memory API Server")
    parser.add_argument("--port", type=int, default=8888, help="Port to listen on")
    parser.add_argument("--db", type=str, default=None, help="Path to memory database")
    args = parser.parse_args()
    
    global _store
    db_path = args.db or os.path.join(os.path.dirname(__file__), "..", "memory_store.db")
    _store = MemoryStore(db_path)
    
    print(f"🧠 Bot-Forge Memory Server")
    print(f"   DB: {_store.db_path}")
    print(f"   Facts: {_store.stats()['total_facts']}")
    print(f"   Listening on http://localhost:{args.port}")
    print()
    print(f"   Endpoints:")
    print(f"     GET  /                  — API info")
    print(f"     GET  /stats             — Memory stats")
    print(f"     GET  /recall/<query>    — Search memories")
    print(f"     POST /store             — Store memory")
    print(f"     POST /feedback/<id>     — Rate helpfulness")
    print()
    
    server = HTTPServer(("0.0.0.0", args.port), MemoryHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down memory server...")
        server.shutdown()


if __name__ == "__main__":
    main()
