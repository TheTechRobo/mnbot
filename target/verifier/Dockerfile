FROM rust:1.82-alpine AS builder

RUN apk add --no-cache musl-dev pkgconfig protobuf-dev git

WORKDIR /usr/src/myapp
RUN git clone https://github.com/TheTechRobo/bullseye /usr/bullseye
COPY . verifier
WORKDIR /usr/src/myapp/verifier

RUN cargo install --path .

ENTRYPOINT ["/usr/local/cargo/bin/verifier"]
