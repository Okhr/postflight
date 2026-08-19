from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings, overridable through `VS_*` environment variables."""

    model_config = SettingsConfigDict(env_prefix="VS_", env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")

    # --- Ingestion -----------------------------------------------------------
    scan_interval_s: float = 30.0
    # How many consecutive scans must see the same size before ingesting:
    # inotify is unreliable on NFS/SMB, so we watch for stability instead.
    stability_checks: int = 2
    video_extensions: str = ".mp4,.mov"
    # Two parts of the same recording are contiguous within this gap.
    split_gap_tolerance_s: float = 2.0
    # A part is only a candidate for having been continued if it got close to the
    # camera's file limit. Measured on a real O3 collection: every genuine split has
    # a first part of 3.763 to 3.770 Go, and every pair wrongly glued by the timing
    # alone has one of 1.398 Go at most. 3 Go sits in the middle with a wide margin on
    # both sides, and stays a setting because the limit belongs to the camera and the
    # card, not to us.
    split_min_part_bytes: int = 3_000_000_000
    # Delete the parts from raw/ after a verified merge (the merge is lossless).
    purge_parts_after_merge: bool = False

    # --- Proxy ---------------------------------------------------------------
    proxy_height: int = 960
    proxy_crf: int = 26
    proxy_x264_preset: str = "veryfast"
    filmstrip_columns: int = 120
    filmstrip_thumb_height: int = 90

    # --- Colour grading ------------------------------------------------------
    # H.264 on purpose: measured 0.71x realtime against 0.26x for HEVC 10-bit on
    # the same clip, and these graded files are the ones meant to be shared.
    grade_crf: int = 20
    grade_x264_preset: str = "veryfast"
    # Frames sampled per second when analysing a clip. 2/s is enough to find the
    # dark, median and bright moments without decoding everything.
    grade_analysis_fps: float = 2.0
    grade_preview_width: int = 960

    # --- Tools ---------------------------------------------------------------
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    gyroflow_bin: str = "gyroflow"
    mp4_merge_bin: str = "mp4_merge"

    # The rush the startup benchmark runs on, baked into the worker image. A real
    # one: a render needs a gyro track, and no tool can synthesize or trim one.
    # Missing (the API image, a dev checkout) simply leaves the worker unranked.
    bench_clip: Path = Path("/opt/bench/clip.mp4")

    # auto | cuda | vaapi | cpu. `auto` probes each backend by really decoding an
    # HEVC 10-bit sample, NVDEC first (a discrete card beats an iGPU on a machine
    # that has both). Anything that fails or hangs is simply not used.
    hwaccel: str = "auto"
    vaapi_device: str = "/dev/dri/renderD128"

    # Gyroflow GPU encoding (AMF/NVENC) is broken on many setups, AMD iGPUs
    # without AMF in particular. Encode on the CPU by default.
    gyroflow_use_gpu_encode: bool = False
    gyroflow_timeout_s: int = 86400

    # --- Worker channel ------------------------------------------------------
    # Where the worker finds the dispatcher. The worker holds no database: this URL
    # is its only link to the rest of the system.
    api_url: str = "http://api:8000"
    # Identity of this worker, stable across restarts so its history stays attached
    # to the machine. Empty means: use the hostname.
    worker_name: str = ""
    # Shared secret on the worker endpoints. Empty leaves them open, which is fine
    # while the API is only reachable on a private network.
    worker_token: str = ""
    # How many heavy jobs one worker runs at once. One on purpose: ffmpeg, mp4_merge
    # and Gyroflow each already saturate every core, so two at a time is slower.
    worker_concurrency: int = 1

    # --- Server --------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    static_dir: Path = Path("/app/static")

    # --- Derived paths -------------------------------------------------------
    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def merged_dir(self) -> Path:
        return self.data_dir / "merged"

    @property
    def proxies_dir(self) -> Path:
        return self.data_dir / "proxies"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"

    @property
    def graded_dir(self) -> Path:
        return self.data_dir / "graded"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def templates_dir(self) -> Path:
        return self.data_dir / "templates"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "video-stab.sqlite3"

    @property
    def extensions(self) -> tuple[str, ...]:
        return tuple(
            e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in self.video_extensions.split(",")
            if e.strip()
        )

    def all_dirs(self) -> list[Path]:
        return [
            self.inbox_dir,
            self.raw_dir,
            self.merged_dir,
            self.proxies_dir,
            self.out_dir,
            self.graded_dir,
            self.projects_dir,
            self.templates_dir,
            self.tmp_dir,
            self.db_path.parent,
        ]

    def ensure_dirs(self) -> None:
        for d in self.all_dirs():
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
