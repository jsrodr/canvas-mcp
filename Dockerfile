# Use Python 3.12 slim image for smaller size.
# Pinned by digest so the build is reproducible; Dependabot's docker
# ecosystem entry keeps the digest current.
FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc

# Set working directory
WORKDIR /app

# Install uv package manager for faster dependency installation
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml ./
COPY LICENSE ./
COPY README.md ./
COPY env.template ./
COPY src/ ./src/

# Copy the auth-gate proxy files (added on top of upstream canvas-mcp)
COPY auth_gate.py ./
COPY start.sh ./
RUN chmod +x start.sh

# Install dependencies using uv. The [hosted] extra (azure-data-tables,
# azure-communication-email, azure-identity) is required by the hosted
# access-approval flow; it is lazily imported, so stdio users are unaffected.
RUN uv pip install --system --no-cache -e ".[hosted]"

# Explicit install for the auth-gate proxy's own dependencies. Harmless if
# these are already pulled in transitively by canvas_mcp/mcp/fastmcp.
RUN uv pip install --system --no-cache starlette uvicorn httpx

# Create non-root user for security
RUN adduser --disabled-password --gecos '' mcp && \
    chown -R mcp:mcp /app

# Set environment variables.
# HTTP deployments pin CANVAS_API_URL at runtime and must NOT set CANVAS_API_TOKEN —
# callers supply their own token per request via the X-Canvas-Token header.
# Code execution (execute_typescript) ships OFF by default for this network-facing
# image; opt in with -e EXECUTE_TYPESCRIPT_ENABLED=true only behind real auth.
# Anonymization ships ON — institutional deployments must opt OUT deliberately
# (set -e ENABLE_DATA_ANONYMIZATION=false) after their own privacy review.
# Example (stdio/local): docker run -e CANVAS_API_TOKEN=xyz -e CANVAS_API_URL=https://... canvas-mcp
#
# GATEWAY_SECRET is required by auth_gate.py - set it in Render's dashboard,
# never in this file.
ENV MCP_SERVER_NAME="canvas-mcp" \
    ENABLE_DATA_ANONYMIZATION="true" \
    ANONYMIZATION_DEBUG="false" \
    EXECUTE_TYPESCRIPT_ENABLED="false"

# Switch to non-root user
USER mcp

# HTTP port the container listens on (App Service injects PORT/WEBSITES_PORT).
# This is now the auth_gate proxy's port, not canvas-mcp-server's - the real
# server listens internally on 8000 and is not directly reachable.
EXPOSE 8819

# Health check to verify installation
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import canvas_mcp; print('OK')" || exit 1

# Run the auth-gate proxy (which itself launches canvas-mcp-server
# internally) instead of running canvas-mcp-server directly.
CMD ["./start.sh"]
