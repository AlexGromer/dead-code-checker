# syntax=docker/dockerfile:1
FROM python:3.12 AS build
WORKDIR /src
COPY . .
RUN ./scripts/build.sh --release

FROM python:3.12
WORKDIR /app
COPY --chmod=755 scripts/entry.sh /usr/local/bin/entry.sh
COPY app/ ./app/
COPY --from=build /out/bin/tool /usr/local/bin/tool
RUN --mount=type=cache,target=/root/.cache \
    python scripts/warm.py && \
    /usr/local/bin/entry.sh --check
RUN <<EOF
./scripts/heredoc.sh
EOF
HEALTHCHECK --interval=5s CMD ["python", "app/health.py"]
ENTRYPOINT ["/usr/local/bin/entry.sh"]
CMD ["python", "app/main.py", "--serve"]
