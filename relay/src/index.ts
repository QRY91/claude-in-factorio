/**
 * Bore Relay — Cloudflare Worker + Durable Object
 *
 * Accepts events from the bridge via POST /ingest (authenticated),
 * fans them out to dashboard viewers via WebSocket on GET /ws.
 * Buffers recent events so late joiners see history.
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
  private buffer: string[] = [];
  private maxBuffer: number;
  private lastEventTime: number = 0;

  constructor(private state: DurableObjectState, private env: Env) {
    this.maxBuffer = parseInt(env.BUFFER_SIZE || "100", 10);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return json({
        status: "ok",
        clients: this.clients.size,
        buffered: this.buffer.length,
        last_event: this.lastEventTime || null,
      });
    }

    if (url.pathname === "/ingest") {
      let events: unknown[];
      try {
        const body = await request.json();
        events = Array.isArray(body) ? body : [body];
      } catch {
        return new Response("Bad JSON", { status: 400 });
      }

      for (const event of events) {
        const data = JSON.stringify(event);
        this.buffer.push(data);
        if (this.buffer.length > this.maxBuffer) {
          this.buffer.shift();
        }
        this.lastEventTime = Date.now();

        // Fan out to all connected viewers
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

      return json({ accepted: events.length, clients: this.clients.size });
    }

    if (url.pathname === "/ws") {
      const pair = new WebSocketPair();
      const [client, server] = [pair[0], pair[1]];

      server.accept();
      this.clients.add(server);

      // Send buffered events to late joiner
      for (const event of this.buffer) {
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
