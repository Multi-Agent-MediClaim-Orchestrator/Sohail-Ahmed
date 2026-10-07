from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PIPELINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class Settings:
    llm_base_url: str = "http://localhost:11434/v1"
    model_local: str = "gemma4:latest"  # pass A and anything that may touch identity
    model_cloud: str = "gemma4:31b-cloud"  # pass B, masked text only
    parser: str = "auto"  # auto | textlayer | tesseract | mineru
    data_dir: Path = field(default_factory=lambda: Path("data/docpipe"))
    pii_key_b64: str = ""
    textlayer_min_chars: int = 200
    parser_min_conf: float = 0.80
    class_min_conf: float = 0.75
    max_pages: int = 30
    max_bytes: int = 25_000_000
    workers: int = 2
    llm_timeout_s: float = 180.0
    allow_cloud: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        g = os.environ.get
        return cls(
            llm_base_url=g("HOSP_LLM_BASE_URL", cls.llm_base_url),
            model_local=g("HOSP_LLM_LOCAL_MODEL", cls.model_local),
            model_cloud=g("HOSP_LLM_MODEL", cls.model_cloud),
            parser=g("DOCPIPE_PARSER", "auto"),
            data_dir=Path(g("DOCPIPE_DATA_DIR", "data/docpipe")),
            pii_key_b64=g("DOCPIPE_PII_KEY_B64", g("HOSP_FIELD_KEY", "")),
            workers=int(g("DOCPIPE_WORKERS", "2")),
            allow_cloud=g("DOCPIPE_ALLOW_CLOUD", "true").lower() == "true",
        )
