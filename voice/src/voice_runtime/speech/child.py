"""Private inference child; stdout is a single bounded JSON result, never diagnostics."""

import base64
import json
import time

from voice_runtime.assets import snapshot
from voice_runtime.profiles import selected_config


def main():
    import sys

    import numpy as np

    request = json.loads(sys.stdin.buffer.read(2 * 1024**2))
    started = time.monotonic()
    engine = request["engine"]
    path = snapshot(engine)
    if engine == "whisper":
        from faster_whisper import WhisperModel

        model = WhisperModel(
            str(path),
            device="cpu",
            compute_type="int8",
            cpu_threads=2,
            num_workers=1,
            local_files_only=True,
        )
        loaded = time.monotonic()
        audio = (
            np.frombuffer(base64.b64decode(request["pcm"]), dtype="<i2").astype(np.float32) / 32768
        )
        mode = request["language_mode"]
        probability = None
        uncertain = False
        if mode == "bilingual":
            # Detect with the actual acoustic model, then restrict the decision to en/ur.
            language, _, scores = model.detect_language(audio)
            allowed = {name: score for name, score in scores if name in ("en", "ur")}
            mode = max(allowed, key=allowed.get)
            probability = allowed[mode]
            uncertain = language not in ("en", "ur") or probability < 0.6
        segments, info = model.transcribe(
            audio,
            language=mode,
            beam_size=5,
            initial_prompt=", ".join(request["public_vocab"]) or None,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        output = {
            "text": text,
            "language": mode,
            "confidence": probability,
            "uncertain": uncertain or not text,
        }
        output["metrics"] = {
            "load_seconds": loaded - started,
            "inference_seconds": time.monotonic() - loaded,
            "device": "cpu",
            "compute_type": "int8",
            "streaming": "utterance",
        }
    else:
        import torch

        config = selected_config("en" if engine == "kokoro" else "ur", request.get("voice_config"))
        torch.set_num_threads(2)
        torch.manual_seed(config["seed"])
        text = request["text"]
        if engine == "kokoro":
            from kokoro import KModel, KPipeline

            model = (
                KModel(
                    repo_id="hexgrad/Kokoro-82M",
                    config=str(path / "config.json"),
                    model=str(path / "kokoro-v1_0.pth"),
                )
                .to("cpu")
                .eval()
            )
            pipeline = KPipeline(
                lang_code="a", repo_id="hexgrad/Kokoro-82M", model=model, device="cpu"
            )
            voice = torch.load(
                path / "voices" / (config["name"] + ".pt"),
                weights_only=True,
                map_location="cpu",
            )
            loaded = time.monotonic()
            arrays = []
            first = None
            with torch.inference_mode():
                for result in pipeline(text, voice=voice, speed=config["speed"]):
                    if first is None:
                        first = time.monotonic() - started
                    arrays.append(result.audio.numpy())
            audio = np.concatenate(arrays)
            rate = 24000
        elif engine == "parler":
            from parler_tts import ParlerTTSForConditionalGeneration
            from transformers import AutoTokenizer

            model = (
                ParlerTTSForConditionalGeneration.from_pretrained(
                    str(path),
                    local_files_only=True,
                    torch_dtype=torch.float32,
                    low_cpu_mem_usage=True,
                )
                .to("cpu")
                .eval()
            )
            tokenizer = AutoTokenizer.from_pretrained(str(path), local_files_only=True)
            # Explicit separate pinned description tokenizer; never resolve a moving model name.
            description_path = snapshot("description")
            description_tokenizer = AutoTokenizer.from_pretrained(
                str(description_path), local_files_only=True
            )
            description = config["description"]
            caption = description_tokenizer(description, return_tensors="pt")
            prompt = tokenizer(text, return_tensors="pt")
            loaded = time.monotonic()
            with torch.inference_mode():
                generated = model.generate(
                    input_ids=caption.input_ids,
                    attention_mask=caption.attention_mask,
                    prompt_input_ids=prompt.input_ids,
                    prompt_attention_mask=prompt.attention_mask,
                    do_sample=config["do_sample"],
                    max_new_tokens=config["max_new_tokens"],
                )
            audio = generated.cpu().numpy().squeeze()
            rate = model.config.sampling_rate
            first = time.monotonic() - started
        else:
            raise ValueError("unsupported engine")
        if audio.ndim != 1 or not np.isfinite(audio).all() or not np.any(audio):
            raise ValueError("invalid or silent synthesis")
        pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
        output = {
            "pcm": base64.b64encode(pcm).decode(),
            "sample_rate": rate,
            "metrics": {
                "load_seconds": loaded - started,
                "first_pcm_seconds": first,
                "inference_seconds": time.monotonic() - loaded,
                "device": "cpu",
                "streaming": "completed-segment",
                "seed": 42,
            },
        }
    sys.stdout.write(json.dumps(output))


if __name__ == "__main__":
    main()
