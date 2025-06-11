FROM python:3.11-bookworm

# Add user for Chromium to run as. (It's unhappy as root.)
RUN groupadd --gid 1000 mnbot \
		&& useradd --uid 1000 --gid 1000 -m mnbot

RUN apt-get update \
		&& DEBIAN_FRONTEND=noninteractive apt-get -y install \
			tini chromium libjpeg-dev python3-dev python3-pip xfonts-base sudo fonts-noto-color-emoji fonts-noto-core fonts-noto-mono fonts-noto-cjk \
		&& echo mnbot ALL=\(root\) NOPASSWD:ALL > /etc/sudoers.d/mnbot \
		&& chmod 0440 /etc/sudoers.d/mnbot \
		&& apt-get clean

# a665d49bba2db8a4094d075e1e5c0803198745fc is known to work.
RUN pip3 install --break-system-packages rethinkdb git+https://github.com/internetarchive/brozzler@a665d49bba2db8a4094d075e1e5c0803198745fc websockets doublethink yt-dlp aiofiles
RUN pip3 install --break-system-packages --upgrade rethinkdb

RUN mkdir /app
COPY app.py /app
COPY browse.py /app
COPY shared.py /app
COPY tracker.py /app
COPY result.py /app

USER mnbot
#ENV DISPLAY x:2.0

# This caused me about three hours of hair-tearing.
# GitHub's homepage didn't work, but everything else did.
# Suspected WebGL, and I was right! https://stackoverflow.com/a/75859549/9654083
# But keeping WebGL enabled in software mode both uses tons of CPU and for some reason hangs the first screenshot attempt.
# Example site: GitHub's homepage.
# So we just disable WebGL entirely. It's not like we really need it for the most part.
ENV BROZZLER_EXTRA_CHROME_ARGS "--in-process-gpu --disable-webgl --disable-3d-apis"

STOPSIGNAL SIGINT
LABEL com.centurylinklabs.watchtower.stop-signal="SIGINT"

ENTRYPOINT ["/usr/bin/tini", "--", "/app/app.py"]

