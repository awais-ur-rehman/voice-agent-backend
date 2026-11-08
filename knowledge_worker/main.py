"""Opt-in bounded polling. Disabled startup has no database or tokenizer side effects."""

import argparse
import json
import signal
import threading

from .settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if not settings.enabled:
        print(json.dumps({"status": "disabled"}), flush=True)
        while not args.once and not stop.wait(30):
            pass
        return

    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    from knowledge.storage import BlobStore

    from .cleanup import cleanup_blobs
    from .runner import run_once
    from .settings import PrivateEmbeddings, load_tokenizer

    # Do not accept owner/admin credentials even if the environment is misconfigured.
    if not settings.database_url or make_url(settings.database_url).username != "knowledge_worker":
        raise SystemExit("worker_database_required")
    engine = create_engine(
        settings.database_url,
        hide_parameters=True,
        pool_size=2,
        max_overflow=0,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=5000 -c lock_timeout=3000",
        },
    )
    factory = sessionmaker(engine, expire_on_commit=False)
    if settings.blob_secret and settings.blob_secret == settings.embedding_secret:
        raise SystemExit("distinct_blob_secret_required")
    store = BlobStore(settings.blob_root, secret=settings.blob_secret)
    embeddings = PrivateEmbeddings(settings.embedding_url, settings.embedding_secret)
    tokenizer = None

    def tokenizer_loader():
        nonlocal tokenizer
        if tokenizer is None:
            tokenizer = load_tokenizer(settings.tokenizer)
        return tokenizer

    from .readiness import heartbeat

    control = threading.Thread(
        target=heartbeat,
        args=(stop, factory, settings, embeddings),
        daemon=True,
        name="knowledge-readiness",
    )
    control.start()
    try:
        while not stop.is_set():
            try:
                run_once(factory, store, embeddings, tokenizer_loader=tokenizer_loader)
                cleanup_blobs(factory, store)
            except Exception:
                # No provider bodies, source text, URL or credential interpolation.
                print(json.dumps({"status": "processing_unavailable"}), flush=True)
            if args.once:
                break
            stop.wait(10)
    finally:
        stop.set()
        control.join(timeout=3)
        engine.dispose()


if __name__ == "__main__":
    main()
