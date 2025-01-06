FROM rust:1.82-alpine AS builder

RUN apk add --no-cache musl-dev pkgconfig protobuf-dev git

WORKDIR /usr/src/myapp
RUN git clone https://github.com/TheTechRobo/bullseye /usr/bullseye
COPY . packer
WORKDIR /usr/src/myapp/packer

RUN cargo install --path .

ENTRYPOINT ["/usr/local/cargo/bin/packer"]
