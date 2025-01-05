FROM debian:bookworm

RUN apt update && apt install -y tini tigervnc-standalone-server && apt clean && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/bin/Xtigervnc", ":2", "-SecurityTypes", "None", "-geometry", "1600x1000", \
	"-depth", "24", "AcceptCutText=0", "AcceptPointerEvents=0", "-ac", \
	"AcceptKeyEvents=0", "-listen", "tcp"]

