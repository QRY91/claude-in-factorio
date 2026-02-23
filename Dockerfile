# Dockerfile for Claude-in-Factorio agent bridge
#
# Containment layer: agents run inside this container with restricted
# filesystem and network access. They can only reach the Factorio
# server via RCON and have no access to the host filesystem.
#
# Build:
#   docker build -t claude-factorio-bridge .
#
# Run:
#   docker run --rm -it \
#     --network host \
#     -e ANTHROPIC_API_KEY \
#     -e RELAY_URL -e RELAY_TOKEN \
#     -v /path/to/script-output:/data/script-output \
#     claude-factorio-bridge \
#     --group doug-squad --scale 1

FROM python:3.12-slim

# Claude CLI requires Node.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Install Claude CLI
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user for the agent
RUN useradd -m -s /bin/bash agent

# Set up directory structure
WORKDIR /app

# Copy bridge (Python, pure stdlib — no pip install needed)
COPY bridge/ /app/bridge/

# Copy factorioctl MCP binary (pre-compiled on host)
COPY factorioctl/target/release/mcp /app/factorioctl/target/release/mcp
RUN chmod +x /app/factorioctl/target/release/mcp

# Copy mod source (for --sync-mod if needed)
COPY mod/ /app/mod/

# Writable directories for the agent
RUN mkdir -p /app/logs /data/script-output && \
    chown -R agent:agent /app /data

# Switch to non-root
USER agent

# Script-output is mounted from host (Factorio writes here)
# Bridge watches /data/script-output/claude-chat/input.jsonl
ENV FACTORIO_SCRIPT_OUTPUT=/data/script-output

ENTRYPOINT ["python3", "/app/bridge/pipe.py", \
    "--script-output", "/data/script-output", \
    "--sse"]
