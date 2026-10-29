"""Explicit single-call native worker entry point and short-lived readiness pulse."""

import argparse
import asyncio
import hashlib
import json
import uuid

import httpx

from voice_runtime.assets import ARTIFACTS, require_manifest_file, snapshot
from voice_runtime.config import BACKEND, LOCAL


def gate_fingerprint(language, voice_profile=None):
    names = ["whisper", "kokoro" if language == "en" else "parler"]
    if language == "ur":
        names.append("description")
    from voice_runtime.profiles import selected_config

    result = {
        "artifacts": {name: ARTIFACTS[name].revision for name in names},
        "voice_config": selected_config(language, voice_profile),
        "locks": {
            str(path.relative_to(BACKEND)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (BACKEND / "voice/uv.lock", BACKEND / "voice/inference/uv.lock")
        },
    }

    if voice_profile:
        result["profile_fingerprint"] = voice_profile["config_fingerprint"]
        if language == "en":
            relative = "voices/" + voice_profile["config"]["name"] + ".pt"
            file = require_manifest_file("kokoro", relative)
            with file.open("rb") as source:
                result["selected_tensor"] = {
                    "file": relative,
                    "sha256": hashlib.file_digest(source, "sha256").hexdigest(),
                }
    return result


def capabilities():
    result = {"en": False, "ur": False}
    # Asset presence alone does not establish a working speech-to-speech pipeline.
    for language, evidence in [("en", "english-loopback.json"), ("ur", "urdu-loopback.json")]:
        path = LOCAL / evidence
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text())
            if record.get("status") != "passed" or record.get("fingerprint") != gate_fingerprint(
                language
            ):
                continue
            snapshot("whisper")
            snapshot("kokoro" if language == "en" else "parler")
            if language == "ur":
                snapshot("description")
            result[language] = True
        except (OSError, ValueError, TypeError):
            pass
    return result


def qualified_profiles():
    """New selections require separate exact-profile evidence; legacy gates cannot qualify them."""
    qualified = []
    for profile in json.loads((BACKEND / "voice/catalog.json").read_text()):
        path = LOCAL / ("profile-" + profile["config_fingerprint"] + "-loopback.json")
        try:
            evidence = json.loads(path.read_text())
            language = profile["language"]
            if evidence.get("status") != "passed" or evidence.get(
                "fingerprint"
            ) != gate_fingerprint(language, profile):
                continue
            snapshot("whisper")
            snapshot(profile["engine"])
            if language == "en":
                require_manifest_file("kokoro", "voices/" + profile["config"]["name"] + ".pt")
            if language == "ur":
                snapshot("description")
            qualified.append(profile["config_fingerprint"])
        except (OSError, ValueError, TypeError):
            continue
    return qualified


async def serve(base_url, bootstrap):
    from loguru import logger

    # Transport libraries may log app-message contents at debug level.
    logger.remove()
    from voice_runtime.pipeline import NativeCall

    worker_id = str(uuid.uuid4())
    headers = {"Authorization": "Bearer " + bootstrap}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10) as client:
        caps = capabilities()
        if not any(caps.values()):
            raise RuntimeError("No real speech capability gate has passed")

        from voice_runtime.cache import prepare_public_cache

        await asyncio.to_thread(prepare_public_cache, caps)

        async def ready():
            while True:
                response = await client.post(
                    "/voice/worker/ready", json=caps | {"profiles": qualified_profiles()}
                )
                response.raise_for_status()
                await asyncio.sleep(5)

        pulse = asyncio.create_task(ready())
        try:
            while True:
                if pulse.done():
                    await pulse
                response = await client.post("/voice/worker/next", json={"worker_id": worker_id})
                response.raise_for_status()
                grant = response.json()
                if grant:
                    language = grant["language_mode"]
                    if language == "ur" and not caps["ur"]:
                        raise RuntimeError("Urdu speech gate unavailable")
                    await NativeCall(base_url, grant).run()
                else:
                    await asyncio.sleep(1)
        finally:
            pulse.cancel()


def main():
    from dotenv import dotenv_values

    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    secret = dotenv_values(BACKEND / ".env.api").get("SCHEDULER_VOICE_BOOTSTRAP_SECRET")
    if not secret:
        raise SystemExit("SCHEDULER_VOICE_BOOTSTRAP_SECRET is missing from backend/.env.api")
    try:
        asyncio.run(serve(args.api_url, secret))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        # No upstream exception text/headers/token URLs in default logs.
        raise SystemExit(type(error).__name__) from None


if __name__ == "__main__":
    main()
