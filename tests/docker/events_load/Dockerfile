# The events load generator and the openssl it needs to turn issued .p12 files into PEM (README.md).
#
#   docker build -t events-load tests/docker/events_load
#   docker run --rm --network host -v "$PWD/load:/work" events-load run -base https://... -accounts /work/accounts ...
#   docker run --rm -v "$PWD/load/accounts:/accounts" --entrypoint p12_to_pem.sh events-load /accounts
FROM golang:1.24-alpine AS build
WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 go vet ./... && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /events_load .

FROM alpine:3.20
RUN apk add --no-cache ca-certificates openssl
COPY --from=build /events_load /usr/local/bin/events_load
COPY p12_to_pem.sh /usr/local/bin/p12_to_pem.sh
RUN chmod 0755 /usr/local/bin/p12_to_pem.sh
WORKDIR /work
ENTRYPOINT ["events_load"]
