ARG BASE=debian:12
FROM golang:1.22 AS Builder
WORKDIR /src
COPY --from=vendor /deps /deps
RUN --mount=type=bind,from=assets,target=/a go build -o /out/app ./cmd/app

FROM builder AS test
RUN go test ./...

FROM scratch AS unused-tools
COPY --from=0 /out /tools

FROM debian:12
COPY --from=builder /out/app /usr/local/bin/app
ENTRYPOINT ["/usr/local/bin/app"]
