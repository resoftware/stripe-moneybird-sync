# syntax=docker/dockerfile:1
FROM python:3.13-alpine@sha256:2d9aefe2fef018a7eb2c13064c89c71929800fd2e5dccdbf52ea5da5bb8d929a
RUN apk add --no-cache tzdata ca-certificates \
    && addgroup -g 10001 stripesync \
    && adduser -D -H -u 10001 -G stripesync stripesync
WORKDIR /app
COPY stripe_moneybird /app/stripe_moneybird
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-m", "stripe_moneybird"]
CMD ["sync"]
LABEL org.opencontainers.image.title="stripe-moneybird-sync"
LABEL org.opencontainers.image.description="Weekly Stripe to Moneybird invoice sync"
