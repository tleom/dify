FROM docker:27.5.1-cli AS docker-cli
FROM python:3.12.10-slim
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY server.py file_ops.py resource_ops.py environment.py stop_jobs.py office_preview.py planning.py planning_exec.py ./
COPY mcp_ops.py mcp_worker.py mcp_runtime.py mcp_journal.py ./
EXPOSE 5010
CMD ["python", "server.py"]
