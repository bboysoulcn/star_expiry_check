#!/bin/sh
apk add gcc python3-dev linux-headers libc-dev libffi libffi-dev --no-cache && \
pip install poetry==1.7.0 && \
poetry install && \
poetry run python main.py
