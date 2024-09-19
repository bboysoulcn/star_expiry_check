FROM python:3.11-alpine
COPY . .
CMD ["sh","./entrypoint.sh"]