# MinIO stopped publishing server images (minio/minio is gone from Docker Hub; quay.io/minio needs a login), so the
# server is built from source at a pinned community release. Multi-arch by construction: builds natively on
# Apple Silicon (arm64) and on x86_64. First build takes a few minutes; Docker caches it afterwards.
ARG MINIO_RELEASE=RELEASE.2025-04-22T22-12-26Z

FROM golang:1.24-alpine AS build
ARG MINIO_RELEASE
ENV CGO_ENABLED=0 GOTOOLCHAIN=auto GOFLAGS=-mod=mod
RUN go install "github.com/minio/minio@${MINIO_RELEASE}"

FROM alpine:3.20
RUN apk add --no-cache ca-certificates wget
COPY --from=build /go/bin/minio /usr/local/bin/minio
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
