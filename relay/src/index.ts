/**
 * Bore Relay — Cloudflare Worker + Durable Object
 *
 * Accepts events from the bridge via POST /ingest (authenticated),
 * fans them out to dashboard viewers via WebSocket on GET /ws.
 * Persists events in SQLite so history survives DO eviction and deploys.
 */

interface Env {
  BORE_RELAY: DurableObjectNamespace;
  RELAY_TOKEN: string;
  BUFFER_SIZE: string;
}

// ── Worker entrypoint ───────────────────────────────────────

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: corsHeaders(),
      });
    }

    if (url.pathname === "/health") {
      const id = env.BORE_RELAY.idFromName("default");
      const stub = env.BORE_RELAY.get(id);
      return stub.fetch(new Request(url.origin + "/health"));
    }

    if (url.pathname === "/ingest" && request.method === "POST") {
      const auth = request.headers.get("Authorization");
      if (!env.RELAY_TOKEN || auth !== `Bearer ${env.RELAY_TOKEN}`) {
        return new Response("Unauthorized", { status: 401 });
      }
      const id = env.BORE_RELAY.idFromName("default");
      const stub = env.BORE_RELAY.get(id);
      return stub.fetch(request);
    }

    if (url.pathname === "/ws") {
      if (request.headers.get("Upgrade") !== "websocket") {
        return new Response("Expected WebSocket", { status: 426 });
      }
      const id = env.BORE_RELAY.idFromName("default");
      const stub = env.BORE_RELAY.get(id);
      return stub.fetch(request);
    }

    return new Response("Not Found", { status: 404 });
  },
};

// ── Durable Object ──────────────────────────────────────────

export class BoreRelay implements DurableObject {
  private clients: Set<WebSocket> = new Set();
  private cache: string[] = [];
  private maxCache: number;
  private initialized = false;

  constructor(private ctx: DurableObjectState, private env: Env) {
    this.maxCache = parseInt(env.BUFFER_SIZE || "200", 10);
  }

  private ensureTable() {
    if (this.initialized) return;
    this.ctx.storage.sql.exec(`
      CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data TEXT NOT NULL,
        ts INTEGER NOT NULL
      )
    `);
    this.initialized = true;
  }

  private hydrate() {
    this.ensureTable();
    if (this.cache.length > 0) return;
    const rows = this.ctx.storage.sql.exec(
      `SELECT data FROM events ORDER BY id DESC LIMIT ?`,
      this.maxCache
    );
    const results: string[] = [];
    for (const row of rows) {
      results.push(row.data as string);
    }
    this.cache = results.reverse();
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      this.ensureTable();
      const countRow = [...this.ctx.storage.sql.exec(`SELECT COUNT(*) as c FROM events`)];
      const total = countRow[0]?.c ?? 0;
      return json({
        status: "ok",
        clients: this.clients.size,
        cached: this.cache.length,
        total_events: total,
      });
    }

    if (url.pathname === "/ingest") {
      this.ensureTable();
      let events: unknown[];
      try {
        const body = await request.json();
        events = Array.isArray(body) ? body : [body];
      } catch {
        return new Response("Bad JSON", { status: 400 });
      }

      const now = Date.now();
      for (const event of events) {
        const data = JSON.stringify(event);

        // Persist to SQLite
        this.ctx.storage.sql.exec(
          `INSERT INTO events (data, ts) VALUES (?, ?)`,
          data, now
        );

        // Update in-memory cache
        this.cache.push(data);
        if (this.cache.length > this.maxCache) {
          this.cache.shift();
        }

        // Fan out to connected viewers
        const dead: WebSocket[] = [];
        for (const ws of this.clients) {
          try {
            ws.send(data);
          } catch {
            dead.push(ws);
          }
        }
        for (const ws of dead) {
          this.clients.delete(ws);
        }
      }

      // Prune old events (keep last 5000)
      this.ctx.storage.sql.exec(
        `DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 5000)`
      );

      return json({ accepted: events.length, clients: this.clients.size });
    }

    if (url.pathname === "/ws") {
      this.hydrate();
      const pair = new WebSocketPair();
      const [client, server] = [pair[0], pair[1]];

      server.accept();
      this.clients.add(server);

      // Replay history to late joiner
      for (const event of this.cache) {
        try {
          server.send(event);
        } catch {
          break;
        }
      }

      server.addEventListener("close", () => {
        this.clients.delete(server);
      });

      server.addEventListener("error", () => {
        this.clients.delete(server);
      });

      return new Response(null, {
        status: 101,
        webSocket: client,
      });
    }

    return new Response("Not Found", { status: 404 });
  }
}

// ── Helpers ─────────────────────────────────────────────────

function corsHeaders(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
  };
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      ...corsHeaders(),
    },
  });
}
