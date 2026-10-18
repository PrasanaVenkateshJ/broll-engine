"""Modal image build step 1: download every AI model file ONCE, into the image (HF_HOME / TORCH_HOME).

It does NOT import our app code on purpose: this step runs BEFORE the code is copied into the image, so
Modal can cache it. Changing app code then does not download ~1.5 GB of models again on every deploy.
The model names come from env vars set by modal_app.py (the same values the running app gets).

Step 2 (after the code is copied) runs deploy/modal/preload_models.py with HF_HUB_OFFLINE=1: it loads
every model the way the app does WITHOUT network, so a file missing here fails the build, not a job.
"""
import os
import time


def step(name, fn):
    start = time.perf_counter()
    fn()
    print(f"download: {name} ok ({time.perf_counter() - start:.1f} s)", flush=True)


def whisper():
    from faster_whisper.utils import download_model  # the same download faster-whisper does on first use

    download_model(os.environ["WHISPER_MODEL"])


def kokoro():
    from huggingface_hub import snapshot_download

    # The model, its config and EVERY voice file (the voice map may name any of them).
    snapshot_download(os.environ["KOKORO_REPO_ID"], allow_patterns=["config.json", "*.pth", "voices/*.pt"])


def clip():
    import open_clip

    open_clip.create_model_and_transforms(os.environ["CLIP_MODEL"], pretrained=os.environ["CLIP_PRETRAINED"])


def spacy_model():
    import spacy

    spacy.load("en_core_web_sm")  # installed as a pinned wheel by requirements-ml.txt; this proves it loads


if __name__ == "__main__":
    step(f"whisper {os.environ['WHISPER_MODEL']}", whisper)
    step(f"kokoro {os.environ['KOKORO_REPO_ID']}", kokoro)
    step(f"open_clip {os.environ['CLIP_MODEL']} / {os.environ['CLIP_PRETRAINED']}", clip)
    step("spacy en_core_web_sm", spacy_model)
